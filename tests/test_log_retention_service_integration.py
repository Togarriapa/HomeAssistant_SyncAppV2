from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from ha_syncapp import log_sync_service, retrigger_cycle
from ha_syncapp.candidate_detection import CandidateDetectionResult, CandidateObservation
from ha_syncapp.database_retention_work import DatabaseRetentionPassResult
from ha_syncapp.database_sync_retrigger import DatabaseSyncRetriggerResult
from ha_syncapp.local_sync_retrigger import LocalSyncRetriggerResult
from ha_syncapp.log_collection import LogCollectionResult
from ha_syncapp.log_retention_work import LogRetentionPassResult
from ha_syncapp.log_sync_process import LogSyncProcessResult
from ha_syncapp.log_sync_retrigger import LogSyncRetriggerResult
from ha_syncapp.runtime_sync_retrigger import RuntimeSyncRetriggerResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
GITHUB_TOKEN = "github-secret-sentinel"
CORE_TOKEN = "supervisor-secret-sentinel"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    return store


def test_normal_log_service_runs_retention_after_log_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    calls: list[tuple[object, ...]] = []
    collection = cast(LogCollectionResult, object())
    processed = LogSyncProcessResult(None)
    retention = LogRetentionPassResult(recovered_interrupted=0, processed=None)

    monkeypatch.setattr(
        log_sync_service,
        "collect_and_enqueue_supervisor_logs",
        lambda *args, **kwargs: calls.append(("collect",)) or collection,
    )
    monkeypatch.setattr(
        log_sync_service,
        "run_log_sync_process",
        lambda *args, **kwargs: calls.append(("sync",)) or processed,
    )

    def run_retention(*args: object, **kwargs: object) -> LogRetentionPassResult:
        calls.append(("retention", *args, kwargs))
        return retention

    monkeypatch.setattr(log_sync_service, "run_log_retention_work_pass", run_retention)
    service = log_sync_service.LogSyncService(
        store,
        tmp_path / "log-artifacts",
        tmp_path / "log-snapshots",
        tmp_path / "log-workspaces",
        TARGET,
        GITHUB_TOKEN,
        core_token=CORE_TOKEN,
        interval_seconds=60.0,
    )
    try:
        service.start(0.0)
        result = service.tick(60.0)
    finally:
        store.__exit__(None, None, None)

    assert [call[0] for call in calls] == ["collect", "sync", "retention"]
    assert result.retention is retention
    retention_call = calls[2]
    assert retention_call[1] is store
    assert retention_call[2] == tmp_path / "log-workspaces" / "retention"
    assert retention_call[3:5] == (TARGET, GITHUB_TOKEN)
    kwargs = cast(dict[str, object], retention_call[5])
    assert kwargs["recover_interrupted"] is False
    assert isinstance(kwargs["reference_time"], datetime)
    assert cast(datetime, kwargs["reference_time"]).tzinfo is UTC


def test_retrigger_cycle_recovers_logs_retention_before_fresh_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    retention = LogRetentionPassResult(recovered_interrupted=1, processed=None)

    monkeypatch.setattr(
        retrigger_cycle,
        "run_local_sync_retrigger_pass",
        lambda *args, **kwargs: LocalSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_database_sync_retrigger_pass",
        lambda *args, **kwargs: DatabaseSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_database_retention_work_pass",
        lambda *args, **kwargs: DatabaseRetentionPassResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_runtime_sync_retrigger_pass",
        lambda *args, **kwargs: RuntimeSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_log_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("log_sync") or LogSyncRetriggerResult(0, None),
    )

    def run_retention(*args: object, **kwargs: object) -> LogRetentionPassResult:
        calls.append("log_retention")
        assert args[0] is store
        assert args[1] == tmp_path / "log-workspaces" / "retention"
        assert args[2:4] == (TARGET, GITHUB_TOKEN)
        assert kwargs["recover_interrupted"] is True
        reference_time = kwargs["reference_time"]
        assert isinstance(reference_time, datetime)
        assert reference_time.tzinfo is UTC
        return retention

    monkeypatch.setattr(retrigger_cycle, "run_log_retention_work_pass", run_retention)
    monkeypatch.setattr(
        retrigger_cycle,
        "detect_and_enqueue_trusted_candidate",
        lambda *args, **kwargs: CandidateDetectionResult(
            observation=CandidateObservation(
                target=TARGET,
                repository_id=123,
                branch="candidate",
                commit_sha=None,
            ),
            work=None,
        ),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "collect_and_enqueue_supervisor_logs",
        lambda *args, **kwargs: calls.append("collect") or cast(LogCollectionResult, object()),
    )

    home = tmp_path / "homeassistant"
    home.mkdir()
    database = home / "home-assistant_v2.db"
    database.touch()
    try:
        result = retrigger_cycle.run_retrigger_cycle(
            store,
            home,
            tmp_path / "snapshots",
            tmp_path / "local-workspaces",
            database,
            tmp_path / "database-staging",
            tmp_path / "database-snapshots",
            tmp_path / "database-workspaces",
            tmp_path / "runtime-staging",
            tmp_path / "runtime-snapshots",
            tmp_path / "runtime-workspaces",
            TARGET,
            GITHUB_TOKEN,
            core_token=CORE_TOKEN,
            log_artifact_root=tmp_path / "log-artifacts",
            log_snapshot_root=tmp_path / "log-snapshots",
            log_workspace_root=tmp_path / "log-workspaces",
            recorder_retention_days=7,
        )
    finally:
        store.__exit__(None, None, None)

    assert calls == ["log_sync", "log_retention", "collect"]
    assert result.log_retention is retention
