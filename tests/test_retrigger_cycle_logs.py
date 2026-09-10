from pathlib import Path

import pytest
from ha_syncapp import retrigger_cycle
from ha_syncapp.candidate_detection import CandidateDetectionResult, CandidateObservation
from ha_syncapp.database_sync_retrigger import DatabaseSyncRetriggerResult
from ha_syncapp.local_sync_retrigger import LocalSyncRetriggerResult
from ha_syncapp.log_collection import LogCollectionError
from ha_syncapp.log_sync_retrigger import LogSyncRetriggerResult
from ha_syncapp.runtime_sync_retrigger import RuntimeSyncRetriggerResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"


def _candidate_absent() -> CandidateDetectionResult:
    return CandidateDetectionResult(
        observation=CandidateObservation(
            target=TARGET,
            repository_id=123,
            branch="candidate",
            commit_sha=None,
        ),
        work=None,
    )


def test_configured_cycle_runs_logs_then_candidate_before_fresh_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    home = tmp_path / "homeassistant"
    home.mkdir()
    database = home / "home-assistant_v2.db"
    database.touch()
    calls: list[str] = []

    monkeypatch.setattr(
        retrigger_cycle,
        "run_local_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("local") or LocalSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_database_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("database") or DatabaseSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_runtime_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("runtime") or RuntimeSyncRetriggerResult(0, None),
    )
    captured: tuple[object, ...] | None = None
    collection_kwargs: dict[str, object] = {}
    collection_result = object()

    def logs(*args: object, **kwargs: object) -> LogSyncRetriggerResult:
        nonlocal captured
        del kwargs
        calls.append("logs")
        captured = args
        return LogSyncRetriggerResult(0, None)

    def candidate(*args: object, **kwargs: object) -> CandidateDetectionResult:
        del args, kwargs
        calls.append("candidate")
        return _candidate_absent()

    def collect(*args: object, **kwargs: object) -> object:
        calls.append("collect")
        collection_kwargs.update(kwargs)
        return collection_result

    monkeypatch.setattr(retrigger_cycle, "run_log_sync_retrigger_pass", logs)
    monkeypatch.setattr(retrigger_cycle, "detect_and_enqueue_trusted_candidate", candidate)
    monkeypatch.setattr(retrigger_cycle, "collect_and_enqueue_supervisor_logs", collect)
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
            "github-token",
            core_token="core-token",
            log_artifact_root=tmp_path / "log-artifacts",
            log_snapshot_root=tmp_path / "log-snapshots",
            log_workspace_root=tmp_path / "log-workspaces",
        )
    finally:
        store.__exit__(None, None, None)

    assert calls == ["local", "database", "runtime", "logs", "candidate", "collect"]
    assert calls.count("logs") == 1
    assert result.log_sync.processed is None
    assert result.candidate_detection.work is None
    assert result.log_collection is collection_result
    assert captured is not None
    assert captured[0] is store
    assert captured[1:4] == (
        tmp_path / "log-artifacts",
        tmp_path / "log-snapshots",
        tmp_path / "log-workspaces",
    )
    assert captured[-2:] == (TARGET, "github-token")
    assert collection_kwargs["token"] == "core-token"
    assert "reference_time" in collection_kwargs


def test_collection_failure_occurs_after_recovery_and_candidate_detection_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    home = tmp_path / "homeassistant"
    home.mkdir()
    database = home / "home-assistant_v2.db"
    database.touch()
    calls: list[str] = []

    monkeypatch.setattr(
        retrigger_cycle,
        "run_local_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("local") or LocalSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_database_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("database") or DatabaseSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_runtime_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("runtime") or RuntimeSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_log_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("logs") or LogSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "detect_and_enqueue_trusted_candidate",
        lambda *args, **kwargs: calls.append("candidate") or _candidate_absent(),
    )

    def collect(*args: object, **kwargs: object) -> object:
        calls.append("collect")
        raise LogCollectionError("nested supervisor-secret-value")

    monkeypatch.setattr(retrigger_cycle, "collect_and_enqueue_supervisor_logs", collect)
    try:
        with pytest.raises(retrigger_cycle.RetriggerCycleError, match="failed closed") as error:
            retrigger_cycle.run_retrigger_cycle(
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
                "github-token",
                core_token="supervisor-secret-value",
                log_artifact_root=tmp_path / "log-artifacts",
                log_snapshot_root=tmp_path / "log-snapshots",
                log_workspace_root=tmp_path / "log-workspaces",
            )
    finally:
        store.__exit__(None, None, None)

    assert calls == ["local", "database", "runtime", "logs", "candidate", "collect"]
    assert "nested supervisor-secret-value" not in str(error.value)
    assert "supervisor-secret-value" not in str(error.value)


def test_cycle_rejects_partial_logs_root_configuration(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    try:
        with pytest.raises(
            retrigger_cycle.RetriggerCycleError,
            match="logs work roots are incomplete",
        ):
            retrigger_cycle.run_retrigger_cycle(
                store,
                tmp_path / "home",
                tmp_path / "snapshots",
                tmp_path / "local-workspaces",
                tmp_path / "home" / "home-assistant_v2.db",
                tmp_path / "database-staging",
                tmp_path / "database-snapshots",
                tmp_path / "database-workspaces",
                tmp_path / "runtime-staging",
                tmp_path / "runtime-snapshots",
                tmp_path / "runtime-workspaces",
                TARGET,
                "github-token",
                log_artifact_root=tmp_path / "log-artifacts",
            )
    finally:
        store.__exit__(None, None, None)
