from pathlib import Path

import pytest
from ha_syncapp import local_sync_process, local_sync_work
from ha_syncapp.local_sync_work import LocalSyncWorkResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    store = StateStore(data)
    store.__enter__()
    return store


def _run(
    store: StateStore,
    tmp_path: Path,
    *,
    target: str = TARGET,
) -> local_sync_process.LocalSyncProcessResult:
    return local_sync_process.run_local_sync_process(
        store,
        tmp_path / "homeassistant",
        tmp_path / "snapshots",
        tmp_path / "workspaces",
        target,
        "github-token",
    )


def test_idle_process_does_not_claim_unrelated_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue_work("candidate", "candidate-sha")
    try:
        result = _run(store, tmp_path)
        unrelated = store.claim_work()
    finally:
        store.__exit__(None, None, None)
    assert result.processed is None
    assert unrelated is not None and unrelated.work_kind == "candidate"


def test_valid_claim_executes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    local_sync_work.enqueue_local_sync_work(store, TARGET)
    calls = 0

    def execute(state: StateStore, item, *args: object, **kwargs: object) -> LocalSyncWorkResult:
        nonlocal calls
        calls += 1
        completed = state.complete_work(item)
        return LocalSyncWorkResult(completed, None)

    monkeypatch.setattr(local_sync_process, "execute_claimed_local_sync_work", execute)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)
    assert calls == 1
    assert result.processed is not None
    assert result.processed.work.status == "succeeded"


def test_mismatched_target_is_blocked_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    local_sync_work.enqueue_local_sync_work(store, TARGET)
    called = False

    def execute(*args: object, **kwargs: object) -> LocalSyncWorkResult:
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(local_sync_process, "execute_claimed_local_sync_work", execute)
    try:
        result = _run(store, tmp_path, target="Owner/Other")
    finally:
        store.__exit__(None, None, None)
    assert called is False
    assert result.processed is not None
    assert result.processed.work.status == "blocked"


def test_normal_process_does_not_recover_interrupted_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    local_sync_work.enqueue_local_sync_work(store, TARGET)
    claimed = local_sync_work.claim_local_sync_work(store)
    assert claimed is not None and claimed.status == "running"
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)
    assert result.processed is None


def test_execution_error_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    local_sync_work.enqueue_local_sync_work(store, TARGET)
    secret = "github-secret"

    def fail(*args: object, **kwargs: object) -> LocalSyncWorkResult:
        raise local_sync_work.LocalSyncWorkError(secret)

    monkeypatch.setattr(local_sync_process, "execute_claimed_local_sync_work", fail)
    try:
        with pytest.raises(local_sync_process.LocalSyncProcessError) as caught:
            _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)
    assert secret not in str(caught.value)
