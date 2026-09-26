from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp.deployment_rollback import DeploymentRollback
from ha_syncapp.deployment_rollback_retrigger import run_deployment_rollback_retrigger_pass
from ha_syncapp.state import StateStore


def _rollback_stub() -> DeploymentRollback:
    rollback = DeploymentRollback.__new__(DeploymentRollback)
    object.__setattr__(rollback, "deployment_id", "rollback-observing")
    object.__setattr__(rollback, "phase", "observing")
    return rollback


def test_observing_recovery_never_replays_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    now = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    rollback = _rollback_stub()
    restore_calls: list[str] = []
    observation_calls: list[str] = []

    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.list_retryable_rollbacks",
        lambda *_args, **_kwargs: [rollback],
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.next_retry_at",
        lambda *_args, **_kwargs: now,
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.execute_rollback_restore",
        lambda *_args, **_kwargs: restore_calls.append("restore") or None,
    )
    monkeypatch.setattr(
        "ha_syncapp.deployment_rollback_retrigger.complete_rollback_observation",
        lambda *_args, **_kwargs: observation_calls.append("observe") or None,
        raising=False,
    )

    try:
        result = run_deployment_rollback_retrigger_pass(
            store,
            "Owner/Private-Home",
            "github-token",
            "core-token",
            reference_time=now,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed == "rollback-observing"
    assert restore_calls == []
    assert observation_calls == ["observe"]
