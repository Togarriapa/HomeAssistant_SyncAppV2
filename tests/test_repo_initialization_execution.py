from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from ha_syncapp.publication_transport import PublicationTransportError
from ha_syncapp.repo_initialization import (
    RepoBInitializationRequest,
    authorize_repo_b_initialization,
    load_repo_b_initialization,
)
from ha_syncapp.repo_initialization_execution import (
    MAX_INITIALIZATION_ATTEMPTS,
    RepoBInitializationExecutionError,
    execute_authorized_repo_b_initialization,
    load_repo_b_initialization_execution,
    next_repo_b_initialization_attempt_at,
)
from ha_syncapp.state import SCHEMA_VERSION, StateStore

NOW = datetime(2026, 9, 29, 11, 30, tzinfo=UTC)
TARGET = "owner/private-repo"
REPOSITORY_ID = 12345
REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"
FINGERPRINT = "SHA256:" + "A" * 43
GENERATION_ID = "223e4567-e89b-42d3-a456-426614174000"
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    authorize_repo_b_initialization(
        store,
        RepoBInitializationRequest(REQUEST_ID, TARGET, REPOSITORY_ID),
        _proof(),
        _snapshot(),
        observed_at=NOW,
        now=NOW,
    )
    return store


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "homeassistant"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    key_directory = tmp_path / "keys"
    for path in (source, snapshots, workspaces, key_directory):
        path.mkdir(mode=0o700)
    (source / "configuration.yaml").write_text("homeassistant:\n  name: Test\n")
    return source, snapshots, workspaces, key_directory


def _proof(
    *,
    fingerprint: str = FINGERPRINT,
    generation: str = GENERATION_ID,
    ref_count: int = 0,
    observation_sha256: str = EMPTY_DIGEST,
):
    return DeployKeyAccessProof(
        TARGET,
        REPOSITORY_ID,
        fingerprint,
        generation,
        ref_count,
        observation_sha256,
    )


def _snapshot(*references: DeployKeyReference) -> DeployKeyReferenceSnapshot:
    raw = b"".join(
        f"{reference.commit_sha}\t{reference.name}\n".encode("ascii") for reference in references
    )
    return DeployKeyReferenceSnapshot(
        TARGET,
        REPOSITORY_ID,
        FINGERPRINT,
        GENERATION_ID,
        references,
        hashlib.sha256(raw).hexdigest(),
    )


def _execute(
    store: StateStore,
    paths: tuple[Path, Path, Path, Path],
    *,
    proof: DeployKeyAccessProof | None = None,
    now: datetime = NOW,
):
    source, snapshots, workspaces, key_directory = paths
    return execute_authorized_repo_b_initialization(
        store,
        REQUEST_ID,
        source,
        snapshots,
        workspaces,
        proof or _proof(),
        key_directory,
        now=now,
    )


def _install_successful_transport(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, object]],
) -> None:
    def read_refs(*args, **kwargs):
        calls.append(("references", kwargs))
        if any(name == "push" for name, _ in calls):
            commit = next(value for name, value in calls if name == "push")
            return _snapshot(DeployKeyReference("refs/heads/main", str(commit)))
        return _snapshot()

    def push(workspace, intent, proof, key_directory, **kwargs):
        calls.append(("push", intent.local_commit_sha))
        assert intent.expect_remote_absent is True
        assert intent.expected_remote_commit_sha is None
        assert intent.branch == "main"
        assert kwargs["require_repository_empty"] is True
        return intent.local_commit_sha

    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
        read_refs,
    )
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.push_publication_intent_with_deploy_key",
        push,
    )


def test_authorized_empty_repository_is_initialized_and_completed_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_successful_transport(monkeypatch, calls)

    try:
        result = _execute(store, paths)
        persisted = load_repo_b_initialization_execution(store, REQUEST_ID)
        authority = load_repo_b_initialization(store, REQUEST_ID)
        baseline = store.synchronization_baseline(TARGET, "main")
    finally:
        store.__exit__(None, None, None)

    assert result == persisted
    assert result.phase == "completed"
    assert result.block_reason == "none"
    assert result.attempt_count == 1
    assert result.terminal_at == NOW
    assert authority is not None and authority.phase == "completed"
    assert baseline is not None
    assert baseline.snapshot_id == result.snapshot_id
    assert baseline.commit_sha == result.commit_sha
    assert [name for name, _ in calls] == ["references", "push", "references"]
    assert list(paths[1].iterdir()) == []
    assert list(paths[2].iterdir()) == []


def test_completed_execution_replays_without_filesystem_network_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_successful_transport(monkeypatch, calls)

    try:
        first = _execute(store, paths)
        paths[0].rename(tmp_path / "source-unavailable")
        monkeypatch.setattr(
            "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
            lambda *a, **k: pytest.fail("completed replay must be offline"),
        )
        replay = _execute(store, paths, now=NOW + timedelta(days=1))
    finally:
        store.__exit__(None, None, None)

    assert replay == first


def test_restart_after_push_reconciles_only_exact_intended_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    remote_commit: list[str] = []

    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
        lambda *a, **k: (
            _snapshot()
            if not remote_commit
            else _snapshot(DeployKeyReference("refs/heads/main", remote_commit[0]))
        ),
    )

    def interrupted_push(workspace, intent, proof, key_directory, **kwargs):
        remote_commit.append(intent.local_commit_sha)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.push_publication_intent_with_deploy_key",
        interrupted_push,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            _execute(store, paths)
        interrupted = load_repo_b_initialization_execution(store, REQUEST_ID)
        assert interrupted is not None and interrupted.phase == "publishing"

        monkeypatch.setattr(
            "ha_syncapp.repo_initialization_execution.push_publication_intent_with_deploy_key",
            lambda *a, **k: pytest.fail("reconciliation must not push twice"),
        )
        recovered = _execute(store, paths, now=NOW + timedelta(minutes=1))
    finally:
        store.__exit__(None, None, None)

    assert recovered.phase == "completed"
    assert recovered.commit_sha == remote_commit[0]
    assert list(paths[1].iterdir()) == []
    assert list(paths[2].iterdir()) == []


def test_transient_transport_failure_is_bounded_and_backed_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    reads = 0

    def read_refs(*args, **kwargs):
        nonlocal reads
        reads += 1
        return _snapshot()

    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
        read_refs,
    )
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.push_publication_intent_with_deploy_key",
        lambda *a, **k: (_ for _ in ()).throw(
            PublicationTransportError("temporary", transient=True)
        ),
    )
    try:
        retry = _execute(store, paths)
        assert retry.phase == "retry"
        assert retry.attempt_count == 1
        assert retry.next_attempt_at == next_repo_b_initialization_attempt_at(retry)
        early = _execute(store, paths, now=retry.next_attempt_at - timedelta(seconds=1))
    finally:
        store.__exit__(None, None, None)

    assert early == retry
    assert reads == 1


def test_changed_source_after_interruption_is_durably_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
        lambda *a, **k: _snapshot(),
    )
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.push_publication_intent_with_deploy_key",
        lambda *a, **k: (_ for _ in ()).throw(
            PublicationTransportError("temporary", transient=True)
        ),
    )
    try:
        retry = _execute(store, paths)
        (paths[0] / "configuration.yaml").write_text("homeassistant:\n  name: Changed\n")
        blocked = _execute(store, paths, now=retry.next_attempt_at)
    finally:
        store.__exit__(None, None, None)

    assert blocked.phase == "blocked"
    assert blocked.block_reason == "source_changed"


def test_changed_key_generation_is_blocked_before_remote_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
        lambda *a, **k: pytest.fail("rebound key must not reach transport"),
    )
    try:
        blocked = _execute(
            store,
            paths,
            proof=_proof(generation="323e4567-e89b-42d3-a456-426614174000"),
        )
    finally:
        store.__exit__(None, None, None)

    assert blocked.phase == "blocked"
    assert blocked.block_reason == "key_changed"


def test_rebound_authority_observation_is_blocked_before_remote_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
        lambda *a, **k: pytest.fail("rebound proof must not reach transport"),
    )
    try:
        blocked = _execute(
            store,
            paths,
            proof=_proof(ref_count=1, observation_sha256="f" * 64),
        )
    finally:
        store.__exit__(None, None, None)

    assert blocked.phase == "blocked"
    assert blocked.block_reason == "key_changed"


def test_nonempty_or_diverged_repository_is_durably_blocked_without_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
        lambda *a, **k: _snapshot(DeployKeyReference("refs/heads/main", "f" * 40)),
    )
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.push_publication_intent_with_deploy_key",
        lambda *a, **k: pytest.fail("divergence must not push"),
    )
    try:
        blocked = _execute(store, paths)
    finally:
        store.__exit__(None, None, None)

    assert blocked.phase == "blocked"
    assert blocked.block_reason == "repository_diverged"


def test_baseline_created_after_authority_blocks_before_remote_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    store.record_synchronization_baseline(TARGET, "main", "b" * 64, "a" * 40, synchronized_at=NOW)
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
        lambda *a, **k: pytest.fail("existing baseline must block before transport"),
    )
    try:
        blocked = _execute(store, paths)
    finally:
        store.__exit__(None, None, None)

    assert blocked.phase == "blocked"
    assert blocked.block_reason == "authority_invalid"


def test_exhausted_transient_attempts_are_durably_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.read_repo_b_deploy_key_references",
        lambda *a, **k: _snapshot(),
    )
    monkeypatch.setattr(
        "ha_syncapp.repo_initialization_execution.push_publication_intent_with_deploy_key",
        lambda *a, **k: (_ for _ in ()).throw(
            PublicationTransportError("temporary", transient=True)
        ),
    )
    current = NOW
    try:
        for _ in range(MAX_INITIALIZATION_ATTEMPTS):
            result = _execute(store, paths, now=current)
            if result.next_attempt_at is not None:
                current = result.next_attempt_at
    finally:
        store.__exit__(None, None, None)

    assert result.phase == "blocked"
    assert result.block_reason == "attempts_exhausted"
    assert result.attempt_count == MAX_INITIALIZATION_ATTEMPTS


def test_tampered_execution_record_fails_closed(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_successful_transport(monkeypatch, calls)
    try:
        _execute(store, paths)
        store._connection.execute(
            "UPDATE repo_b_initialization_execution SET record_sha256=? WHERE request_id=?",
            ("0" * 64, REQUEST_ID),
        )
        with pytest.raises(RepoBInitializationExecutionError, match="failed closed"):
            load_repo_b_initialization_execution(store, REQUEST_ID)
    finally:
        store.__exit__(None, None, None)


def test_execution_journal_contains_no_source_paths_content_or_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    paths = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_successful_transport(monkeypatch, calls)
    try:
        execution = _execute(store, paths)
    finally:
        store.__exit__(None, None, None)

    raw = (tmp_path / "data" / "syncapp" / "state.sqlite3").read_bytes()
    assert b"name: Test" not in raw
    assert str(paths[0]).encode() not in raw
    assert str(paths[3]).encode() not in raw
    assert b"private_key" not in raw
    assert execution.snapshot_id.encode() in raw
    assert execution.commit_sha.encode() in raw


def test_schema_36_migrates_execution_state_without_losing_authority(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        store._connection.execute("DROP TABLE IF EXISTS repo_b_initialization_execution")
        store._connection.execute("PRAGMA user_version = 36")

    with StateStore(tmp_path) as migrated:
        assert SCHEMA_VERSION == 37
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (37,)
        assert migrated.repository_id(TARGET) == REPOSITORY_ID
        assert load_repo_b_initialization_execution(migrated, REQUEST_ID) is None


def test_runtime_inventory_exposes_identity_free_execution_aggregates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_syncapp.retrigger_runtime_status import collect_retrigger_runtime_inventory

    store = _store(tmp_path)
    paths = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_successful_transport(monkeypatch, calls)
    try:
        execution = _execute(store, paths)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    status = inventory.analysis["recovery"]["repo_b_initialization_execution"]
    assert status == {
        "total": 1,
        "phases": {
            "blocked": 0,
            "completed": 1,
            "prepared": 0,
            "publishing": 0,
            "retry": 0,
        },
        "attempts_total": 1,
        "attempts_maximum": 1,
        "latest_updated_at": NOW.isoformat(),
    }
    encoded = str(status)
    for sensitive in (
        TARGET,
        REQUEST_ID,
        FINGERPRINT,
        GENERATION_ID,
        execution.snapshot_id,
        execution.commit_sha,
        execution.record_sha256,
    ):
        assert sensitive not in encoded


def test_runtime_inventory_rejects_future_execution_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_syncapp.retrigger_runtime_status import (
        RetriggerRuntimeStatusError,
        collect_retrigger_runtime_inventory,
    )

    store = _store(tmp_path)
    paths = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_successful_transport(monkeypatch, calls)
    try:
        _execute(store, paths)
        store._connection.execute(
            "UPDATE repo_b_initialization_execution SET updated_at=? WHERE request_id=?",
            ((NOW + timedelta(seconds=1)).isoformat(), REQUEST_ID),
        )
        with pytest.raises(RetriggerRuntimeStatusError, match="invalid"):
            collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)
