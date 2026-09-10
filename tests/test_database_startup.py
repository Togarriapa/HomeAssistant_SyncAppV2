from pathlib import Path

import pytest
from ha_syncapp import database_startup
from ha_syncapp.database_sync_process import (
    DatabaseSyncProcessError,
    DatabaseSyncProcessResult,
)
from ha_syncapp.database_sync_work import database_sync_work_key
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    store = StateStore(data)
    store.__enter__()
    return store


def test_startup_schedules_before_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    source = tmp_path / "homeassistant" / "recorder.db"
    events: list[str] = []
    scheduled_item = None

    def schedule(state: StateStore, target: str, database: Path):
        nonlocal scheduled_item
        assert state is store
        assert target == TARGET
        assert database == source
        events.append("schedule")
        scheduled_item = state.enqueue_work(
            "database", database_sync_work_key(TARGET, source)
        )
        return scheduled_item

    def process(
        state: StateStore,
        database: Path,
        database_staging_root: Path,
        snapshot_staging_root: Path,
        workspace_root: Path,
        target: str,
        github_token: str,
    ) -> DatabaseSyncProcessResult:
        assert state is store
        assert database == source
        assert database_staging_root == tmp_path / "database-staging"
        assert snapshot_staging_root == tmp_path / "snapshots"
        assert workspace_root == tmp_path / "workspaces"
        assert target == TARGET
        assert github_token == "github-token"
        events.append("process")
        return DatabaseSyncProcessResult(None)

    monkeypatch.setattr(database_startup, "schedule_database_sync_generation", schedule)
    monkeypatch.setattr(database_startup, "run_database_sync_process", process)
    try:
        result = database_startup.run_startup_database_sync(
            store,
            source,
            tmp_path / "database-staging",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "github-token",
        )
    finally:
        store.__exit__(None, None, None)

    assert events == ["schedule", "process"]
    assert result.scheduled is scheduled_item
    assert result.processed.processed is None


def test_blocked_database_generation_remains_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = tmp_path / "homeassistant" / "recorder.db"
    key = database_sync_work_key(TARGET, source)
    store.enqueue_work("database", key)
    claimed = store.claim_work_kind("database")
    assert claimed is not None
    blocked = store.fail_work(claimed, transient=False)
    assert blocked.status == "blocked"

    try:
        result = database_startup.run_startup_database_sync(
            store,
            source,
            tmp_path / "database-staging",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "github-token",
        )
    finally:
        store.__exit__(None, None, None)

    assert result.scheduled.status == "blocked"
    assert result.processed.processed is None


def test_processing_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    secret = "github-secret-sentinel"

    def fail(*args: object, **kwargs: object) -> DatabaseSyncProcessResult:
        raise DatabaseSyncProcessError(secret)

    monkeypatch.setattr(database_startup, "run_database_sync_process", fail)
    try:
        with pytest.raises(database_startup.DatabaseStartupError) as error:
            database_startup.run_startup_database_sync(
                store,
                tmp_path / "homeassistant" / "recorder.db",
                tmp_path / "database-staging",
                tmp_path / "snapshots",
                tmp_path / "workspaces",
                TARGET,
                secret,
            )
    finally:
        store.__exit__(None, None, None)

    assert str(error.value) == "startup database synchronization failed closed"
    assert secret not in str(error.value)


def test_unopened_store_fails_closed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)

    with pytest.raises(database_startup.DatabaseStartupError, match="failed closed"):
        database_startup.run_startup_database_sync(
            store,
            tmp_path / "homeassistant" / "recorder.db",
            tmp_path / "database-staging",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "github-token",
        )
