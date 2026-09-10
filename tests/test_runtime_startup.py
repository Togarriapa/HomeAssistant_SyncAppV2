from pathlib import Path

import pytest
from ha_syncapp import runtime_startup
from ha_syncapp.runtime_sync_process import RuntimeSyncProcessError, RuntimeSyncProcessResult
from ha_syncapp.runtime_sync_work import runtime_sync_work_key
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

    def schedule(state: StateStore, target: str):
        nonlocal scheduled_item
        assert state is store
        assert target == TARGET
        events.append("schedule")
        scheduled_item = state.enqueue_work("runtime", runtime_sync_work_key(TARGET))
        return scheduled_item

    def process(
        state: StateStore,
        runtime_staging_root: Path,
        snapshot_staging_root: Path,
        workspace_root: Path,
        target: str,
        github_token: str,
        *,
        core_token: str | None = None,
    ) -> RuntimeSyncProcessResult:
        assert state is store
        assert runtime_staging_root == tmp_path / "runtime"
        assert snapshot_staging_root == tmp_path / "snapshots"
        assert workspace_root == tmp_path / "workspaces"
        assert target == TARGET
        assert github_token == "github-token"
        assert core_token == "core-token"
        events.append("process")
        return RuntimeSyncProcessResult(None)

    monkeypatch.setattr(runtime_startup, "schedule_runtime_sync_generation", schedule)
    monkeypatch.setattr(runtime_startup, "run_runtime_sync_process", process)
    try:
        result = runtime_startup.run_startup_runtime_sync(
            store,
            tmp_path / "runtime",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "github-token",
            core_token="core-token",
        )
    finally:
        store.__exit__(None, None, None)

    assert events == ["schedule", "process"]
    assert result.scheduled is scheduled_item
    assert result.processed.processed is None


def test_blocked_runtime_generation_remains_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    key = runtime_sync_work_key(TARGET)
    pending = store.enqueue_work("runtime", key)
    claimed = store.claim_work_kind("runtime")
    assert claimed is not None
    blocked = store.fail_work(claimed, transient=False)
    assert blocked.status == "blocked"
    process_calls = 0

    def process(*args: object, **kwargs: object) -> RuntimeSyncProcessResult:
        nonlocal process_calls
        process_calls += 1
        return RuntimeSyncProcessResult(None)

    monkeypatch.setattr(runtime_startup, "run_runtime_sync_process", process)
    try:
        result = runtime_startup.run_startup_runtime_sync(
            store,
            tmp_path / "runtime",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "github-token",
        )
    finally:
        store.__exit__(None, None, None)

    assert pending.work_key == key
    assert result.scheduled.status == "blocked"
    assert process_calls == 1


def test_processing_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    secret = "github-secret-sentinel"

    def fail(*args: object, **kwargs: object) -> RuntimeSyncProcessResult:
        raise RuntimeSyncProcessError(secret)

    monkeypatch.setattr(runtime_startup, "run_runtime_sync_process", fail)
    try:
        with pytest.raises(runtime_startup.RuntimeStartupError) as error:
            runtime_startup.run_startup_runtime_sync(
                store,
                tmp_path / "runtime",
                tmp_path / "snapshots",
                tmp_path / "workspaces",
                TARGET,
                secret,
            )
    finally:
        store.__exit__(None, None, None)

    assert str(error.value) == "startup runtime synchronization failed closed"
    assert secret not in str(error.value)


def test_unopened_store_fails_closed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)

    with pytest.raises(runtime_startup.RuntimeStartupError, match="failed closed"):
        runtime_startup.run_startup_runtime_sync(
            store,
            tmp_path / "runtime",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "github-token",
        )
