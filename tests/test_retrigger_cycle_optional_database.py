"""Recovery must not depend on optional Recorder configuration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from ha_syncapp.candidate_fetch_stage_retrigger import CandidateFetchStageRetriggerResult
from ha_syncapp.database_sync_retrigger import DatabaseSyncRetriggerResult
from ha_syncapp.local_sync_retrigger import LocalSyncRetriggerResult
from ha_syncapp.retrigger_cycle import run_retrigger_cycle
from ha_syncapp.runtime_sync_retrigger import RuntimeSyncRetriggerResult
from ha_syncapp.state import StateStore


def test_retrigger_cycle_skips_database_lane_when_source_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    local = LocalSyncRetriggerResult(recovered_interrupted=0, processed=None)
    runtime = RuntimeSyncRetriggerResult(recovered_interrupted=0, processed=None)
    candidate = Mock()

    monkeypatch.setattr(
        "ha_syncapp.retrigger_cycle.run_local_sync_retrigger_pass", Mock(return_value=local)
    )
    database_pass = Mock()
    monkeypatch.setattr(
        "ha_syncapp.retrigger_cycle.run_database_sync_retrigger_pass", database_pass
    )
    monkeypatch.setattr(
        "ha_syncapp.retrigger_cycle.run_runtime_sync_retrigger_pass", Mock(return_value=runtime)
    )
    monkeypatch.setattr(
        "ha_syncapp.retrigger_cycle.run_candidate_fetch_stage_retrigger_pass",
        Mock(return_value=CandidateFetchStageRetriggerResult(0, 0, None)),
    )
    monkeypatch.setattr(
        "ha_syncapp.retrigger_cycle.detect_and_enqueue_trusted_candidate",
        Mock(return_value=candidate),
    )
    try:
        result = run_retrigger_cycle(
            store,
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
    finally:
        store.__exit__(None, None, None)

    database_pass.assert_not_called()
    assert result.database_sync == DatabaseSyncRetriggerResult(
        recovered_interrupted=0, processed=None
    )
