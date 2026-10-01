from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp import deploy_key_retention_authority as authority_module
from ha_syncapp.config import Config
from ha_syncapp.database_history_evidence import (
    DatabaseHistoryRecord,
    validate_trusted_database_history_evidence,
)
from ha_syncapp.database_retention_work import discover_database_retention_work
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.log_history_evidence import (
    LogHistoryRecord,
    validate_trusted_log_history_evidence,
)
from ha_syncapp.log_retention_work import discover_log_retention_work
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
LOG_HEAD = "a" * 40
LOG_ROOT = "b" * 40
DATABASE_HEAD = "c" * 40
DATABASE_ROOT = "d" * 40
PROOF = DeployKeyAccessProof(
    target=TARGET,
    repository_id=REPOSITORY_ID,
    key_fingerprint="SHA256:" + "A" * 43,
    generation_id="123e4567-e89b-42d3-a456-426614174000",
    ref_count=2,
    observation_sha256="e" * 64,
)


def _authority(tmp_path: Path) -> authority_module.DeployKeyRetentionAuthority:
    return authority_module.DeployKeyRetentionAuthority(
        PROOF,
        tmp_path / "key",
        tmp_path / "access",
        tmp_path / "staging",
    )


def _snapshot(*references: DeployKeyReference) -> DeployKeyReferenceSnapshot:
    return DeployKeyReferenceSnapshot(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        key_fingerprint=PROOF.key_fingerprint,
        generation_id=PROOF.generation_id,
        references=tuple(references),
        observation_sha256="f" * 64,
    )


def _log_evidence():
    return validate_trusted_log_history_evidence(
        branch_head=BranchHead(TARGET, REPOSITORY_ID, "logs", LOG_HEAD),
        records=(
            LogHistoryRecord(LOG_HEAD, NOW - timedelta(days=1), (LOG_ROOT,)),
            LogHistoryRecord(LOG_ROOT, NOW - timedelta(days=40), ()),
        ),
        reference_time=NOW,
    )


def _database_evidence():
    return validate_trusted_database_history_evidence(
        branch_head=BranchHead(TARGET, REPOSITORY_ID, "database", DATABASE_HEAD),
        records=(
            DatabaseHistoryRecord(
                DATABASE_HEAD, NOW - timedelta(days=1), (DATABASE_ROOT,)
            ),
            DatabaseHistoryRecord(DATABASE_ROOT, NOW - timedelta(days=10), ()),
        ),
        reference_time=NOW,
        retention_days=7,
    )


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store


@pytest.mark.parametrize(
    ("branch", "expected"),
    [("logs", LOG_HEAD), ("database", DATABASE_HEAD)],
)
def test_authority_observes_exact_generated_branch_from_proof_bound_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    expected: str,
) -> None:
    monkeypatch.setattr(
        authority_module,
        "read_repo_b_deploy_key_references",
        lambda *_args, **_kwargs: _snapshot(
            DeployKeyReference("refs/heads/database", DATABASE_HEAD),
            DeployKeyReference("refs/heads/logs", LOG_HEAD),
        ),
    )

    observed = _authority(tmp_path).observe(TARGET, REPOSITORY_ID, branch)

    assert observed == BranchHead(TARGET, REPOSITORY_ID, branch, expected)


def test_authority_rejects_duplicate_branch_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        authority_module,
        "read_repo_b_deploy_key_references",
        lambda *_args, **_kwargs: _snapshot(
            DeployKeyReference("refs/heads/logs", LOG_HEAD),
            DeployKeyReference("refs/heads/logs", LOG_ROOT),
        ),
    )

    with pytest.raises(authority_module.DeployKeyRetentionAuthorityError):
        _authority(tmp_path).observe(TARGET, REPOSITORY_ID, "logs")


@pytest.mark.parametrize("transient", [False, True])
def test_authority_sanitizes_access_failure_and_preserves_retry_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient: bool
) -> None:
    monkeypatch.setattr(
        authority_module,
        "read_repo_b_deploy_key_references",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DeployKeyAccessError("private-key secret detail", transient=transient)
        ),
    )

    with pytest.raises(authority_module.DeployKeyRetentionAuthorityError) as caught:
        _authority(tmp_path).observe(TARGET, REPOSITORY_ID, "logs")

    assert caught.value.transient is transient
    assert "secret" not in str(caught.value)


def test_logs_discovery_uses_deploy_key_authority_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    authority = _authority(tmp_path)
    calls: list[datetime] = []
    monkeypatch.setattr(
        authority_module.DeployKeyRetentionAuthority,
        "read_log_history",
        lambda _self, reference_time: calls.append(reference_time) or _log_evidence(),
    )
    try:
        item = discover_log_retention_work(
            store,
            TARGET,
            None,
            reference_time=NOW,
            retention_authority=authority,
        )
    finally:
        store.__exit__(None, None, None)

    assert item.work_kind == "logs_retention"
    assert calls == [NOW]


def test_database_discovery_uses_deploy_key_authority_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    authority = _authority(tmp_path)
    calls: list[tuple[datetime, int]] = []
    monkeypatch.setattr(
        authority_module.DeployKeyRetentionAuthority,
        "read_database_history",
        lambda _self, reference_time, retention_days: (
            calls.append((reference_time, retention_days)) or _database_evidence()
        ),
    )
    try:
        item = discover_database_retention_work(
            store,
            TARGET,
            None,
            retention_days=7,
            reference_time=NOW,
            retention_authority=authority,
        )
    finally:
        store.__exit__(None, None, None)

    assert item.work_kind == "database_retention"
    assert calls == [(NOW, 7)]


def test_discovery_rejects_mixed_token_and_deploy_key_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="exactly one"):
            discover_log_retention_work(
                store,
                TARGET,
                "token-secret",
                reference_time=NOW,
                retention_authority=_authority(tmp_path),
            )
    finally:
        store.__exit__(None, None, None)


def test_service_builds_retention_authority_only_after_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    protected = data / "syncapp"
    (protected / "repo-b-deploy-key").mkdir(parents=True, mode=0o700)
    config = Config(
        repo_b=TARGET,
        github_token="rest-identity-token",
        repo_b_retention_transport="deploy_key",
    )
    monkeypatch.setattr(service, "test_repo_b_deploy_key_access", lambda *_a, **_k: PROOF)
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        with pytest.raises(service.RetriggerCycleError, match="initialized"):
            service._deploy_key_retention_authority_if_configured(store, config, data)
        store.record_synchronization_baseline(
            TARGET, "main", "1" * 64, "2" * 40, synchronized_at=NOW
        )
        authority = service._deploy_key_retention_authority_if_configured(
            store, config, data
        )

    assert authority is not None
    assert "rest-identity-token" not in repr(authority)
    assert str(tmp_path) not in repr(authority)
