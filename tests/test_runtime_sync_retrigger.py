from pathlib import Path

import pytest
from ha_syncapp import runtime_sync_retrigger, runtime_sync_work
from ha_syncapp.runtime_sync_process import RuntimeSyncProcessError, RuntimeSyncProcessResult
from ha_syncapp.runtime_sync_work import RuntimeSyncWorkResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    store = StateStore(data)
    store.__enter__()
    return store


def _run(store: StateStore, tmp_path: Path) -> runtime_sync_retrigger.RuntimeSyncRetriggerResult:
    return runtime_sync_retrigger.run_runtime_sync_retrigger_pass(
        store,
        tmp_path / "runtime-staging",
        tmp_path / "snapshots",
        tmp_path / "workspaces",
        TARGET,
        "github-token",
        core_token="core-token",
    )


def test_retrigger_recovers_before_delegating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    claimed = runtime_sync_work.claim_runtime_sync_work(store)
    assert claimed is not None and claimed.status == "running"
    observed_attempts: list[int] = []

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
        del runtime_staging_root, snapshot_staging_root, workspace_root
        assert target == TARGET
        assert github_token == "github-token"
        assert core_token == "core-token"
        item = runtime_sync_work.claim_runtime_sync_work(state)
        assert item is not None
        observed_attempts.append(item.attempts)
        completed = state.complete_work(item)
        return RuntimeSyncProcessResult(RuntimeSyncWorkResult(completed, None))

    monkeypatch.setattr(runtime_sync_retrigger, "run_runtime_sync_process", process)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 1
    assert observed_attempts == [2]
    assert result.processed is not None
    assert result.processed.work.status == "succeeded"


def test_retrigger_delegates_even_when_nothing_was_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    called = 0

    def process(*args: object, **kwargs: object) -> RuntimeSyncProcessResult:
        nonlocal called
        called += 1
        return RuntimeSyncProcessResult(None)

    monkeypatch.setattr(runtime_sync_retrigger, "run_runtime_sync_process", process)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 0
    assert result.processed is None
    assert called == 1


def test_processing_error_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    secret = "secret-sentinel"

    def fail(*args: object, **kwargs: object) -> RuntimeSyncProcessResult:
        raise RuntimeSyncProcessError(secret)

    monkeypatch.setattr(runtime_sync_retrigger, "run_runtime_sync_process", fail)
    try:
        with pytest.raises(runtime_sync_retrigger.RuntimeSyncRetriggerError) as caught:
            _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert str(caught.value) == "runtime synchronization retrigger pass failed closed"
    assert secret not in str(caught.value)


def test_unopened_state_store_fails_closed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)

    with pytest.raises(runtime_sync_retrigger.RuntimeSyncRetriggerError, match="failed closed"):
        _run(store, tmp_path)
