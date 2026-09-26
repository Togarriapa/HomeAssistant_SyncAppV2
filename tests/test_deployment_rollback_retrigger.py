from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.deployment_rollback import DeploymentRollback
from ha_syncapp.deployment_rollback_retrigger import (
    DeploymentRollbackRetriggerResult,
    list_retryable_rollbacks,
    run_deployment_rollback_retrigger_pass,
)
from ha_syncapp.state import StateStore


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _rollback_stub(
    deployment_id: str = "rollback-retrigger-test",
    *,
    phase: str = "uncertain",
) -> DeploymentRollback:
    rollback = DeploymentRollback.__new__(DeploymentRollback)
    object.__setattr__(rollback, "deployment_id", deployment_id)
    object.__setattr__(rollback, "phase", phase)
    return rollback


def _insert_rollback_row(
    store: StateStore,
    deployment_id: str,
    phase: str,
    updated_at: datetime,
) -> None:
    sha = "a" * 64
    commit_a = "b" * 40
    commit_b = "c" * 40
    placeholders = ", ".join("?" for _ in range(17))
    store._connection.execute(
        f"INSERT INTO deployment_rollback VALUES ({placeholders})",
        (
            deployment_id,
            "Owner/Private-Home",
            1,
            commit_a,
            commit_b,
            "backup-1",
            sha,
            sha,
            sha,
            phase,
            "none",
            "none",
            0,
            None,
            updated_at.isoformat(),
            updated_at.isoformat(),
            sha,
        ),
    )
    store._connection.commit()


def test_durable_discovery_returns_only_automatic_recovery_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 9, 20, 20, 0, tzinfo=UTC)
    _insert_rollback_row(store, "planned-work", "planned", now - timedelta(minutes=10))
    _insert_rollback_row(store, "uncertain-work", "uncertain", now - timedelta(minutes=10))
    _insert_rollback_row(store, "completed-work", "completed", now - timedelta(minutes=10))
    _insert_rollback_row(store, "blocked-work", "blocked", now - timedelta(minutes=10))

    def decode(row: tuple[object, ...]) -> DeploymentRollback:
        return _rollback_stub(str(row[0]), phase=str(row[9]))

    monkeypatch.setattr(DeploymentRollback, "from_database_row", decode)
    try:
        discovered = list_retryable_rollbacks(store, now)
    finally:
        store.__exit__(None, None, None)

    assert [item.deployment_id for item in discovered] == ["planned-work", "uncertain-work"]


def test_durable_discovery_is_bounded_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 9, 20, 20, 0, tzinfo=UTC)
    for suffix in ("c", "a", "b"):
        _insert_rollback_row(store, f"rollback-{suffix}", "planned", now - timedelta(minutes=10))

    def decode(row: tuple[object, ...]) -> DeploymentRollback:
        return _rollback_stub(str(row[0]), phase=str(row[9]))

    monkeypatch.setattr(DeploymentRollback, "from_database_row", decode)
    try:
        discovered = list_retryable_rollbacks(store, now)
    finally:
        store.__exit__(None, None, None)

    assert [item.deployment_id for item in discovered] == ["rollback-a", "rollback-b", "rollback-c"]
    assert len(discovered) <= 32


def test_retrigger_pass_is_bounded_and_noops_without_pending_rollback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        result = run_deployment_rollback_retrigger_pass(
            store,
            "Owner/Private-Home",
            "github-token",
            "core-token",
            reference_time=datetime(2026, 9, 20, 20, 0, tzinfo=UTC),
        )
    finally:
        store.__exit__(None, None, None)

    assert result == DeploymentRollbackRetriggerResult(
        recovered_stale_locks=0,
        considered=0,
        processed=None,
    )


def test_retrigger_claims_exact_deployment_identity_before_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 9, 20, 20, 0, tzinfo=UTC)
    rollback = _rollback_stub("rollback-claim")
    reconcile_calls: list[str] = []
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.list_retryable_rollbacks",
        lambda *_args, **_kwargs: [rollback],
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.rollback_requires_reconciliation",
        lambda _rollback: True,
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.reconcile_pending_rollback",
        lambda *_args, **_kwargs: reconcile_calls.append("reconcile") or None,
    )
    try:
        result = run_deployment_rollback_retrigger_pass(
            store,
            "Owner/Private-Home",
            "github-token",
            "core-token",
            reference_time=now,
        )
        work = store._get_work("deployment_rollback", "rollback-claim")
    finally:
        store.__exit__(None, None, None)

    assert result.processed == "rollback-claim"
    assert work.status == "succeeded"
    assert work.attempts == 1
    assert reconcile_calls == ["reconcile"]


def test_retrigger_recovers_only_interrupted_rollback_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 9, 20, 20, 0, tzinfo=UTC)
    rollback = _rollback_stub("rollback-stale")
    store.enqueue_work("deployment_rollback", "rollback-stale", now=now - timedelta(minutes=5))
    claimed = store.claim_work_kind("deployment_rollback", now=now - timedelta(minutes=5))
    assert claimed is not None
    other = store.enqueue_work("logs", "unrelated", now=now - timedelta(minutes=5))
    other_claimed = store.claim_work_kind("logs", now=now - timedelta(minutes=5))
    assert other_claimed is not None
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.list_retryable_rollbacks",
        lambda *_args, **_kwargs: [rollback],
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.rollback_requires_reconciliation",
        lambda _rollback: True,
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.reconcile_pending_rollback",
        lambda *_args, **_kwargs: None,
    )
    try:
        result = run_deployment_rollback_retrigger_pass(
            store,
            "Owner/Private-Home",
            "github-token",
            "core-token",
            reference_time=now,
        )
        rollback_work = store._get_work("deployment_rollback", "rollback-stale")
        unrelated = store._get_work(other.work_kind, other.work_key)
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_stale_locks == 1
    assert rollback_work.status == "succeeded"
    assert rollback_work.attempts == 2
    assert unrelated.status == "running"


def test_retrigger_never_replays_restore_for_uncertain_or_acknowledged_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    restore_calls: list[str] = []
    reconcile_calls: list[str] = []

    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.list_retryable_rollbacks",
        lambda *_args, **_kwargs: [_rollback_stub()],
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.rollback_requires_reconciliation",
        lambda _rollback: True,
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.reconcile_pending_rollback",
        lambda *_args, **_kwargs: reconcile_calls.append("reconcile") or None,
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.execute_rollback_restore",
        lambda *_args, **_kwargs: restore_calls.append("restore") or None,
    )

    try:
        result = run_deployment_rollback_retrigger_pass(
            store,
            "Owner/Private-Home",
            "github-token",
            "core-token",
            reference_time=datetime(2026, 9, 20, 20, 0, tzinfo=UTC),
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed == "rollback-retrigger-test"
    assert reconcile_calls == ["reconcile"]
    assert restore_calls == []


def test_retrigger_skips_deterministically_blocked_candidate_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    mutations: list[str] = []
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.list_retryable_rollbacks",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.execute_rollback_restore",
        lambda *_args, **_kwargs: mutations.append("restore") or None,
    )

    try:
        result = run_deployment_rollback_retrigger_pass(
            store,
            "Owner/Private-Home",
            "github-token",
            "core-token",
            reference_time=datetime(2026, 9, 20, 20, 0, tzinfo=UTC),
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed is None
    assert mutations == []
