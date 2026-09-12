from pathlib import Path

import pytest
from ha_syncapp import database_sync_service as database_service
from ha_syncapp.database_sync_process import DatabaseSyncProcessResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Home"
TOKEN = "github-secret-sentinel"


def _opened_store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    return store


def _service(tmp_path: Path, store: StateStore, *, interval: float = 60.0) -> database_service.DatabaseSyncService:
    return database_service.DatabaseSyncService(
        store,
        tmp_path / "homeassistant" / "recorder.db",
        tmp_path / "database-staging",
        tmp_path / "database-snapshots",
        tmp_path / "database-workspaces",
        TARGET,
        TOKEN,
        interval_seconds=interval,
    )


def test_database_service_arms_first_deadline_without_immediate_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        database_service,
        "schedule_database_sync_generation",
        lambda *args, **kwargs: calls.append("schedule"),
    )
    monkeypatch.setattr(
        database_service,
        "run_database_sync_process",
        lambda *args, **kwargs: calls.append("process") or DatabaseSyncProcessResult(None),
    )
    service = _service(tmp_path, store)
    try:
        service.start(100.0)
        assert service.tick(159.999) == database_service.DatabaseSyncTickResult(
            due=False,
            processed=None,
        )
        assert calls == []
    finally:
        store.__exit__(None, None, None)


def test_database_service_due_tick_schedules_then_processes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[tuple[object, ...]] = []

    def schedule(state: StateStore, target: str, source: Path) -> None:
        calls.append(("schedule", state, target, source))

    def process(
        state: StateStore,
        source: Path,
        database_staging: Path,
        snapshot_staging: Path,
        workspace: Path,
        target: str,
        token: str,
    ) -> DatabaseSyncProcessResult:
        calls.append(
            (
                "process",
                state,
                source,
                database_staging,
                snapshot_staging,
                workspace,
                target,
                token,
            )
        )
        return DatabaseSyncProcessResult(None)

    monkeypatch.setattr(database_service, "schedule_database_sync_generation", schedule)
    monkeypatch.setattr(database_service, "run_database_sync_process", process)
    service = _service(tmp_path, store)
    source = tmp_path / "homeassistant" / "recorder.db"
    try:
        service.start(10.0)
        result = service.tick(70.0)
        assert result == database_service.DatabaseSyncTickResult(
            due=True,
            processed=DatabaseSyncProcessResult(None),
        )
        assert calls == [
            ("schedule", store, TARGET, source),
            (
                "process",
                store,
                source,
                tmp_path / "database-staging",
                tmp_path / "database-snapshots",
                tmp_path / "database-workspaces",
                TARGET,
                TOKEN,
            ),
        ]
    finally:
        store.__exit__(None, None, None)


def test_database_service_coalesces_missed_intervals_without_catchup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        database_service,
        "schedule_database_sync_generation",
        lambda *args, **kwargs: calls.append("schedule"),
    )
    monkeypatch.setattr(
        database_service,
        "run_database_sync_process",
        lambda *args, **kwargs: calls.append("process") or DatabaseSyncProcessResult(None),
    )
    service = _service(tmp_path, store, interval=30.0)
    try:
        service.start(0.0)
        assert service.tick(300.0).due is True
        assert service.tick(300.0).due is False
        assert service.tick(329.999).due is False
        assert service.tick(330.0).due is True
        assert calls == ["schedule", "process", "schedule", "process"]
    finally:
        store.__exit__(None, None, None)


def test_database_service_rejects_invalid_clock_and_lifecycle(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    service = _service(tmp_path, store)
    try:
        with pytest.raises(database_service.DatabaseSyncServiceError, match="not started"):
            service.tick(0.0)
        with pytest.raises(database_service.DatabaseSyncServiceError, match="clock"):
            service.start(float("nan"))
        service.start(0.0)
        with pytest.raises(database_service.DatabaseSyncServiceError, match="already started"):
            service.start(1.0)
    finally:
        store.__exit__(None, None, None)


def test_database_service_wraps_schedule_failure_without_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    processed = False

    def fail_schedule(*args: object, **kwargs: object) -> None:
        raise database_service.DatabaseSyncScheduleError("sentinel")

    def forbidden_process(*args: object, **kwargs: object) -> DatabaseSyncProcessResult:
        nonlocal processed
        processed = True
        return DatabaseSyncProcessResult(None)

    monkeypatch.setattr(database_service, "schedule_database_sync_generation", fail_schedule)
    monkeypatch.setattr(database_service, "run_database_sync_process", forbidden_process)
    service = _service(tmp_path, store)
    try:
        service.start(0.0)
        with pytest.raises(database_service.DatabaseSyncServiceError, match="tick failed closed"):
            service.tick(60.0)
        assert processed is False
    finally:
        store.__exit__(None, None, None)
