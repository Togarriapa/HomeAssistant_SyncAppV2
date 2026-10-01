from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from ha_syncapp.repo_initialization import (
    RepoBInitializationError,
    RepoBInitializationRequest,
    authorize_repo_b_initialization,
    discover_authorized_repo_b_initializations,
    load_repo_b_initialization,
)
from ha_syncapp.state import SCHEMA_VERSION, StateStore

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)
TARGET = "owner/private-repo"
REPOSITORY_ID = 12345
REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"
SECOND_REQUEST_ID = "123e4567-e89b-42d3-a456-426614174001"
FINGERPRINT = "SHA256:" + "A" * 43
GENERATION_ID = "223e4567-e89b-42d3-a456-426614174000"
EMPTY_OBSERVATION = hashlib.sha256(b"").hexdigest()


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store


def _request(
    *,
    request_id: str = REQUEST_ID,
    target: str = TARGET,
    repository_id: int = REPOSITORY_ID,
) -> RepoBInitializationRequest:
    return RepoBInitializationRequest(request_id, target, repository_id)


def _proof(
    *,
    target: str = TARGET,
    repository_id: int = REPOSITORY_ID,
    key_fingerprint: str = FINGERPRINT,
    generation_id: str = GENERATION_ID,
    ref_count: int = 0,
    observation_sha256: str = EMPTY_OBSERVATION,
) -> DeployKeyAccessProof:
    return DeployKeyAccessProof(
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        ref_count,
        observation_sha256,
    )


def _snapshot(
    *,
    target: str = TARGET,
    repository_id: int = REPOSITORY_ID,
    key_fingerprint: str = FINGERPRINT,
    generation_id: str = GENERATION_ID,
    references: tuple[DeployKeyReference, ...] = (),
    observation_sha256: str = EMPTY_OBSERVATION,
) -> DeployKeyReferenceSnapshot:
    return DeployKeyReferenceSnapshot(
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        references,
        observation_sha256,
    )


def _authorize(
    store: StateStore,
    request: RepoBInitializationRequest | None = None,
    proof: DeployKeyAccessProof | None = None,
    snapshot: DeployKeyReferenceSnapshot | None = None,
    *,
    observed_at: datetime | None = NOW,
    now: datetime = NOW,
):
    return authorize_repo_b_initialization(
        store,
        request or _request(),
        proof,
        snapshot,
        observed_at=observed_at,
        now=now,
    )


def test_empty_repo_explicit_request_is_authorized_and_discoverable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        authority = _authorize(store, proof=_proof(), snapshot=_snapshot())
        loaded = load_repo_b_initialization(store, REQUEST_ID)
        discovered = discover_authorized_repo_b_initializations(store)
    finally:
        store.__exit__(None, None, None)

    assert authority == loaded
    assert discovered == (authority,)
    assert authority.phase == "authorized"
    assert authority.block_reason == "none"
    assert authority.observed_at == NOW
    assert authority.recorded_at == NOW
    assert authority.terminal_at is None
    assert len(authority.record_sha256) == 64


def test_exact_request_replays_from_journal_without_remote_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        first = _authorize(store, proof=_proof(), snapshot=_snapshot())
        replay = _authorize(
            store,
            observed_at=None,
            now=NOW + timedelta(days=1),
        )
        count = store._connection.execute("SELECT COUNT(*) FROM repo_b_initialization").fetchone()
    finally:
        store.__exit__(None, None, None)

    assert replay == first
    assert count == (1,)


@pytest.mark.parametrize(
    "initialization_request",
    [
        _request(target="other/repository"),
        _request(repository_id=54321),
    ],
)
def test_request_id_cannot_be_rebound(tmp_path: Path, initialization_request) -> None:
    store = _store(tmp_path)
    try:
        _authorize(store, proof=_proof(), snapshot=_snapshot())
        with pytest.raises(RepoBInitializationError, match="failed closed"):
            _authorize(
                store,
                request=initialization_request,
                observed_at=None,
                now=NOW,
            )
    finally:
        store.__exit__(None, None, None)


def test_request_id_cannot_be_rebound_to_new_key_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _authorize(store, proof=_proof(), snapshot=_snapshot())
        changed_fingerprint = "SHA256:" + "B" * 43
        with pytest.raises(RepoBInitializationError, match="failed closed"):
            _authorize(
                store,
                proof=_proof(key_fingerprint=changed_fingerprint),
                snapshot=_snapshot(key_fingerprint=changed_fingerprint),
            )
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "initialization_request",
    [
        _request(request_id=REQUEST_ID.upper()),
        _request(target="owner//repository"),
        _request(repository_id=True),
    ],
)
def test_noncanonical_request_is_rejected_without_journal(
    tmp_path: Path,
    initialization_request: RepoBInitializationRequest,
) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(RepoBInitializationError, match="failed closed"):
            _authorize(
                store,
                request=initialization_request,
                proof=_proof(),
                snapshot=_snapshot(),
            )
        count = store._connection.execute("SELECT COUNT(*) FROM repo_b_initialization").fetchone()
    finally:
        store.__exit__(None, None, None)

    assert count == (0,)


def test_nonempty_repo_is_durably_blocked_and_never_discovered(tmp_path: Path) -> None:
    reference = DeployKeyReference("refs/heads/main", "a" * 40)
    raw = f"{'a' * 40}\trefs/heads/main\n".encode()
    digest = hashlib.sha256(raw).hexdigest()
    proof = _proof(ref_count=1, observation_sha256=digest)
    snapshot = _snapshot(references=(reference,), observation_sha256=digest)
    store = _store(tmp_path)
    try:
        blocked = _authorize(store, proof=proof, snapshot=snapshot)
        replay = _authorize(store, observed_at=None, now=NOW + timedelta(days=1))
        discovered = discover_authorized_repo_b_initializations(store)
    finally:
        store.__exit__(None, None, None)

    assert blocked == replay
    assert blocked.phase == "blocked"
    assert blocked.block_reason == "repository_not_empty"
    assert blocked.terminal_at == NOW
    assert discovered == ()


def test_existing_baseline_is_durably_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.record_synchronization_baseline(
            TARGET,
            "main",
            "b" * 64,
            "a" * 40,
            synchronized_at=NOW - timedelta(minutes=1),
        )
        blocked = _authorize(store, proof=_proof(), snapshot=_snapshot())
    finally:
        store.__exit__(None, None, None)

    assert blocked.phase == "blocked"
    assert blocked.block_reason == "already_initialized"


def test_competing_active_request_is_durably_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        first = _authorize(store, proof=_proof(), snapshot=_snapshot())
        second = _authorize(
            store,
            request=_request(request_id=SECOND_REQUEST_ID),
            proof=_proof(),
            snapshot=_snapshot(),
        )
        discovered = discover_authorized_repo_b_initializations(store)
    finally:
        store.__exit__(None, None, None)

    assert second.phase == "blocked"
    assert second.block_reason == "active_request"
    assert discovered == (first,)


@pytest.mark.parametrize(
    ("proof", "snapshot", "observed_at"),
    [
        (_proof(target="other/repository"), _snapshot(), NOW),
        (_proof(), _snapshot(repository_id=54321), NOW),
        (_proof(key_fingerprint="SHA256:" + "B" * 43), _snapshot(), NOW),
        (_proof(generation_id=SECOND_REQUEST_ID), _snapshot(), NOW),
        (_proof(), _snapshot(observation_sha256="0" * 64), NOW),
        (_proof(), _snapshot(), NOW - timedelta(minutes=6)),
        (_proof(), _snapshot(), NOW + timedelta(seconds=31)),
    ],
)
def test_malformed_rebound_or_stale_evidence_fails_closed(
    tmp_path: Path,
    proof: DeployKeyAccessProof,
    snapshot: DeployKeyReferenceSnapshot,
    observed_at: datetime,
) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(RepoBInitializationError, match="failed closed"):
            _authorize(
                store,
                proof=proof,
                snapshot=snapshot,
                observed_at=observed_at,
            )
        assert load_repo_b_initialization(store, REQUEST_ID) is None
    finally:
        store.__exit__(None, None, None)


def test_tampered_journal_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _authorize(store, proof=_proof(), snapshot=_snapshot())
        store._connection.execute(
            "UPDATE repo_b_initialization SET record_sha256=? WHERE request_id=?",
            ("0" * 64, REQUEST_ID),
        )
        with pytest.raises(RepoBInitializationError, match="failed closed"):
            load_repo_b_initialization(store, REQUEST_ID)
        with pytest.raises(RepoBInitializationError, match="failed closed"):
            discover_authorized_repo_b_initializations(store)
    finally:
        store.__exit__(None, None, None)


def test_authorized_discovery_is_bounded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        for index in range(17):
            target = f"owner/private-repo-{index}"
            request_id = f"123e4567-e89b-42d3-a456-4266141740{index:02d}"
            repository_id = REPOSITORY_ID + index + 1
            store.bind_repository(target, repository_id)
            _authorize(
                store,
                request=_request(
                    request_id=request_id,
                    target=target,
                    repository_id=repository_id,
                ),
                proof=_proof(target=target, repository_id=repository_id),
                snapshot=_snapshot(target=target, repository_id=repository_id),
            )
        with pytest.raises(RepoBInitializationError, match="failed closed"):
            discover_authorized_repo_b_initializations(store)
    finally:
        store.__exit__(None, None, None)


def test_authorization_failure_rolls_back_journal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store._connection.execute(
            "CREATE TRIGGER reject_initialization BEFORE INSERT ON repo_b_initialization "
            "BEGIN SELECT RAISE(ABORT, 'secret storage detail'); END"
        )
        with pytest.raises(RepoBInitializationError, match="failed closed") as error:
            _authorize(store, proof=_proof(), snapshot=_snapshot())
        count = store._connection.execute("SELECT COUNT(*) FROM repo_b_initialization").fetchone()
    finally:
        store.__exit__(None, None, None)

    assert count == (0,)
    assert "secret storage detail" not in str(error.value)


def test_journal_contains_no_refs_commits_or_credentials(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        authority = _authorize(store, proof=_proof(), snapshot=_snapshot())
    finally:
        store.__exit__(None, None, None)
    raw = (tmp_path / "syncapp" / "state.sqlite3").read_bytes()

    assert b"refs/" not in raw
    assert b"github_pat_" not in raw
    assert authority.observation_sha256.encode() in raw


def test_schema_35_migrates_authority_without_losing_existing_state(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        store._connection.execute("DROP TABLE IF EXISTS repo_b_initialization")
        store._connection.execute("PRAGMA user_version = 35")

    with StateStore(tmp_path) as migrated:
        assert SCHEMA_VERSION == 38
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (38,)
        assert migrated.repository_id(TARGET) == REPOSITORY_ID
        assert load_repo_b_initialization(migrated, REQUEST_ID) is None


def test_runtime_inventory_exposes_only_aggregate_initialization_status(
    tmp_path: Path,
) -> None:
    from ha_syncapp.retrigger_runtime_status import collect_retrigger_runtime_inventory

    store = _store(tmp_path)
    try:
        authority = _authorize(store, proof=_proof(), snapshot=_snapshot())
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    status = inventory.analysis["recovery"]["repo_b_initialization"]
    assert status == {
        "total": 1,
        "phases": {"authorized": 1, "blocked": 0, "completed": 0},
        "block_reasons": {
            "active_request": 0,
            "already_initialized": 0,
            "execution_blocked": 0,
            "none": 1,
            "repository_not_empty": 0,
        },
        "latest_recorded_at": NOW.isoformat(),
    }
    encoded = json.dumps(inventory.analysis["recovery"], sort_keys=True)
    for sensitive in (
        TARGET,
        REQUEST_ID,
        str(REPOSITORY_ID),
        FINGERPRINT,
        GENERATION_ID,
        authority.observation_sha256,
        authority.record_sha256,
    ):
        assert sensitive not in encoded


def test_runtime_inventory_rejects_future_initialization_evidence(tmp_path: Path) -> None:
    from ha_syncapp.retrigger_runtime_status import (
        RetriggerRuntimeStatusError,
        collect_retrigger_runtime_inventory,
    )

    store = _store(tmp_path)
    try:
        _authorize(store, proof=_proof(), snapshot=_snapshot())
        store._connection.execute(
            "UPDATE repo_b_initialization SET recorded_at=? WHERE request_id=?",
            ((NOW + timedelta(seconds=1)).isoformat(), REQUEST_ID),
        )
        with pytest.raises(RepoBInitializationError, match="failed closed"):
            load_repo_b_initialization(store, REQUEST_ID)
        with pytest.raises(RetriggerRuntimeStatusError, match="invalid"):
            collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)
