"""Recovery must not depend on optional Recorder configuration."""

from __future__ import annotations

from unittest.mock import Mock

from ha_syncapp.database_sync_retrigger import DatabaseSyncRetriggerResult
from ha_syncapp.retrigger_cycle import run_retrigger_cycle


def test_retrigger_cycle_skips_database_lane_when_source_is_unconfigured(monkeypatch, tmp_path, state_store) -> None:
    local = Mock(recovered_interrupted=0, processed=None)
    runtime = Mock(recovered_interrupted=0, processed=None)
    logs = Mock(recovered_interrupted=0, processed=None)
    candidate = Mock()

    monkeypatch.setattr("ha_syncapp.retrigger_cycle.run_local_sync_retrigger_pass", Mock(return_value=local))
    database_pass = Mock()
    monkeypatch.setattr("ha_syncapp.retrigger_cycle.run_database_sync_retrigger_pass", database_pass)
    monkeypatch.setattr("ha_syncapp.retrigger_cycle.run_runtime_sync_retrigger_pass", Mock(return_value=runtime))
    monkeypatch.setattr("ha_syncapp.retrigger_cycle.run_log_sync_retrigger_pass", Mock(return_value=logs))
    monkeypatch.setattr("ha_syncapp.retrigger_cycle.detect_and_enqueue_trusted_candidate", Mock(return_value=candidate))
    monkeypatch.setattr("ha_syncapp.retrigger_cycle.collect_and_enqueue_supervisor_logs", Mock(return_value=None))

    result = run_retrigger_cycle(
        state_store,
        tmp_path / "homeassistant",
        tmp_path / "main-snapshots",
        tmp_path / "main-workspaces",
        None,
        tmp_path / "database-staging",
        tmp_path / "database-snapshots",
        tmp_path / "database-workspaces",
        tmp_path / "runtime-staging",
        tmp_path / "runtime-snapshots",
        tmp_path / "runtime-workspaces",
        "owner/repo",
        "token",
    )

    database_pass.assert_not_called()
    assert result.database_sync == DatabaseSyncRetriggerResult(recovered_interrupted=0, processed=None)
