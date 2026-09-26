from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.deployment_rollback import DeploymentRollback, RollbackRestoreResult
from ha_syncapp.deployment_rollback_retrigger import (
    DeploymentRollbackRetriggerError,
    run_deployment_rollback_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2026, 9, 26, 11, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _rollback(*, phase: str = "planned") -> DeploymentRollback:
    rollback = DeploymentRollback.__new__(DeploymentRollback)
    object.__setattr__(rollback, "deployment_id", "rollback-classification")
    object.__setattr__(rollback, "phase", phase)
    object.__setattr__(rollback, "attempt_count", 1)
    object.__setattr__(rollback, "updated_at", NOW - timedelta(hours=1))
    return rollback


def _prepare(monkeypatch: pytest.MonkeyPatch, rollback: DeploymentRollback) -> None:
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.list_retryable_rollbacks",
        lambda *_args, **_kwargs: [rollback],
    )


@pytest.mark.parametrize("transient, expected", [(True, "retry"), (False, "blocked")])
def test_failure_classification_controls_durable_retry_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transient: bool,
    expected: str,
) -> None:
    store = _store(tmp_path)
    rollback = _rollback()
    _prepare(monkeypatch, rollback)

    def fail(*_args: object, **_kwargs: object) -> RollbackRestoreResult:
        raise DeploymentRollbackRetriggerError("sensitive detail", transient=transient)

    monkeypatch.setattr("ha_syncapp.deployment_rollback_retrigger.execute_rollback_restore", fail)
    try:
        with pytest.raises(DeploymentRollbackRetriggerError):
            run_deployment_rollback_retrigger_pass(
                store,
                "Owner/Private-Home",
                "github-token",
                "core-token",
                reference_time=NOW,
            )
        work = store._get_work("deployment_rollback", rollback.deployment_id)
    finally:
        store.__exit__(None, None, None)

    assert work.status == expected
    assert (work.next_attempt_at is not None) is transient


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("restore_acknowledged", "retry"),
        ("reconciliation_required", "retry"),
        ("in_progress", "retry"),
        ("restored", "retry"),
        ("blocked", "blocked"),
        ("ambiguous", "blocked"),
        ("completed", "succeeded"),
    ],
)
def test_domain_outcome_controls_durable_work_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected: str,
) -> None:
    store = _store(tmp_path)
    phase = "observing" if outcome == "completed" else "planned"
    rollback = _rollback(phase=phase)
    _prepare(monkeypatch, rollback)
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.complete_rollback_observation",
        lambda *_args, **_kwargs: RollbackRestoreResult(outcome, False),
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.execute_rollback_restore",
        lambda *_args, **_kwargs: RollbackRestoreResult(outcome, False),
    )
    try:
        result = run_deployment_rollback_retrigger_pass(
            store,
            "Owner/Private-Home",
            "github-token",
            "core-token",
            reference_time=NOW,
        )
        work = store._get_work("deployment_rollback", rollback.deployment_id)
    finally:
        store.__exit__(None, None, None)

    assert result.processed == rollback.deployment_id
    assert work.status == expected


def test_no_pending_rollback_requires_no_supervisor_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.list_retryable_rollbacks",
        lambda *_args, **_kwargs: [],
    )
    try:
        result = run_deployment_rollback_retrigger_pass(
            store,
            "Owner/Private-Home",
            "github-token",
            None,
            reference_time=NOW,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed is None
