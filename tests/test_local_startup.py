from pathlib import Path

import pytest
from ha_syncapp import local_startup, local_sync_work
from ha_syncapp.local_sync import LocalSyncError
from ha_syncapp.local_sync_process import LocalSyncProcessError, LocalSyncProcessResult
from ha_syncapp.local_sync_work import local_sync_work_key
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
    events: list[str] = []
    scheduled_item = None

    def schedule(state: StateStore, target: str, *, branch: str = "main"):
        nonlocal scheduled_item
        assert state is store
        assert target == TARGET
        assert branch == "main"
        events.append("schedule")
        scheduled_item = state.enqueue_work("local_sync", local_sync_work_key(TARGET))
        return scheduled_item

    def process(
        state: StateStore,
        source: Path,
        snapshot_root: Path,
        workspace_root: Path,
        target: str,
        github_token: str,
        *,
        branch: str = "main",
    ) -> LocalSyncProcessResult:
        assert state is store
        assert source == tmp_path / "homeassistant"
        assert snapshot_root == tmp_path / "snapshots"
        assert workspace_root == tmp_path / "workspaces"
        assert target == TARGET
        assert github_token == "github-token"
        assert branch == "main"
        events.append("process")
        return LocalSyncProcessResult(None)

    monkeypatch.setattr(local_startup, "schedule_local_sync_generation", schedule)
    monkeypatch.setattr(local_startup, "run_local_sync_process", process)
    try:
        result = local_startup.run_startup_local_sync(
            store,
            tmp_path / "homeassistant",
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


def test_transient_local_failure_remains_durable_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    secret = "github-secret-sentinel"

    def fail(*args: object, **kwargs: object):
        raise LocalSyncError(f"failed with {secret}")

    monkeypatch.setattr(local_sync_work, "synchronize_local_configuration", fail)
    try:
        result = local_startup.run_startup_local_sync(
            store,
            tmp_path / "homeassistant",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            secret,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.scheduled.status == "pending"
    assert result.processed.processed is not None
    assert result.processed.processed.work.status == "retry"
    assert result.processed.processed.work.next_attempt_at is not None
    assert secret not in repr(result)


def test_blocked_local_generation_remains_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = local_sync_work_key(TARGET)
    store.enqueue_work("local_sync", key)
    claimed = store.claim_work_kind("local_sync")
    assert claimed is not None
    blocked = store.fail_work(claimed, transient=False)
    assert blocked.status == "blocked"

    try:
        result = local_startup.run_startup_local_sync(
            store,
            tmp_path / "homeassistant",
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

    def fail(*args: object, **kwargs: object) -> LocalSyncProcessResult:
        raise LocalSyncProcessError(secret)

    monkeypatch.setattr(local_startup, "run_local_sync_process", fail)
    try:
        with pytest.raises(local_startup.LocalStartupError) as error:
            local_startup.run_startup_local_sync(
                store,
                tmp_path / "homeassistant",
                tmp_path / "snapshots",
                tmp_path / "workspaces",
                TARGET,
                secret,
            )
    finally:
        store.__exit__(None, None, None)

    assert str(error.value) == "startup Local synchronization failed closed"
    assert secret not in str(error.value)


def test_unopened_store_fails_closed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)

    with pytest.raises(local_startup.LocalStartupError, match="failed closed"):
        local_startup.run_startup_local_sync(
            store,
            tmp_path / "homeassistant",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "github-token",
        )
