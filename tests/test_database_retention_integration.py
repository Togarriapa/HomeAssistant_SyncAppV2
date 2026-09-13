from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from ha_syncapp import database_sync_service, retrigger_cycle
from ha_syncapp.candidate_detection import CandidateDetectionResult, CandidateObservation
from ha_syncapp.database_retention_work import DatabaseRetentionPassResult
from ha_syncapp.database_sync_process import DatabaseSyncProcessResult
from ha_syncapp.database_sync_retrigger import DatabaseSyncRetriggerResult
from ha_syncapp.local_sync_retrigger import LocalSyncRetriggerResult
from ha_syncapp.runtime_sync_retrigger import RuntimeSyncRetriggerResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    return store


def test_periodic_database_flow_discovers_and_processes_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        database_sync_service,
        "schedule_database_sync_generation",
        lambda *args, **kwargs: calls.append("snapshot_schedule"),
    )
    monkeypatch.setattr(
        database_sync_service,
        "run_database_sync_process",
        lambda *args, **kwargs: calls.append("snapshot_process") or DatabaseSyncProcessResult(None),
    )
    monkeypatch.setattr(
        database_sync_service,
        "run_database_retention_work_pass",
        lambda *args, **kwargs: calls.append("retention") or DatabaseRetentionPassResult(0, None),
    )
    service = database_sync_service.DatabaseSyncService(
        store,
        tmp_path / "homeassistant" / "recorder.db",
        tmp_path / "database-staging",
        tmp_path / "database-snapshots",
        tmp_path / "database-workspaces",
        TARGET,
        "github-token",
        interval_seconds=60,
        retention_days=7,
    )
    try:
        service.start(0)
        result = service.tick(60)
    finally:
        store.__exit__(None, None, None)

    assert calls == ["snapshot_schedule", "snapshot_process", "retention"]
    assert result.retention == DatabaseRetentionPassResult(0, None)


def test_retrigger_runs_retention_after_snapshot_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    candidate = CandidateDetectionResult(CandidateObservation(TARGET, 123, "candidate", None), None)
    monkeypatch.setattr(
        retrigger_cycle,
        "run_local_sync_retrigger_pass",
        lambda *args, **kwargs: LocalSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_database_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("snapshot") or DatabaseSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_database_retention_work_pass",
        lambda *args, **kwargs: calls.append("retention") or DatabaseRetentionPassResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_runtime_sync_retrigger_pass",
        lambda *args, **kwargs: RuntimeSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle, "detect_and_enqueue_trusted_candidate", Mock(return_value=candidate)
    )
    home = tmp_path / "homeassistant"
    home.mkdir()
    recorder = home / "recorder.db"
    recorder.touch()
    try:
        result = retrigger_cycle.run_retrigger_cycle(
            store,
            home,
            tmp_path / "main-snapshots",
            tmp_path / "main-workspaces",
            recorder,
            tmp_path / "database-staging",
            tmp_path / "database-snapshots",
            tmp_path / "database-workspaces",
            tmp_path / "runtime-staging",
            tmp_path / "runtime-snapshots",
            tmp_path / "runtime-workspaces",
            TARGET,
            "github-token",
            recorder_retention_days=7,
            retention_reference_time=datetime(2026, 9, 13, tzinfo=UTC),
        )
    finally:
        store.__exit__(None, None, None)

    assert calls == ["snapshot", "retention"]
    assert result.database_retention == DatabaseRetentionPassResult(0, None)
