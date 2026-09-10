from pathlib import Path

import pytest
from ha_syncapp import database_sync_process, database_sync_work
from ha_syncapp.database_sync_work import DatabaseSyncWorkResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
DATABASE = Path("/homeassistant/home-assistant_v2.db")


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
    source_database: Path = DATABASE,
) -> database_sync_process.DatabaseSyncProcessResult:
    return database_sync_process.run_database_sync_process(
        store,
        source_database,
        tmp_path / "database-staging",
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
    database_sync_work.enqueue_database_sync_work(store, TARGET, DATABASE)
    calls = 0

    def execute(
        state: StateStore,
        item,
        *args: object,
        **kwargs: object,
    ) -> DatabaseSyncWorkResult:
        nonlocal calls
        calls += 1
        completed = state.complete_work(item)
        return DatabaseSyncWorkResult(completed, None)

    monkeypatch.setattr(database_sync_process, "execute_claimed_database_sync_work", execute)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)
    assert calls == 1
    assert result.processed is not None
    assert result.processed.work.status == "succeeded"


def test_mismatched_database_is_blocked_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, DATABASE)
    called = False

    def execute(*args: object, **kwargs: object) -> DatabaseSyncWorkResult:
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(database_sync_process, "execute_claimed_database_sync_work", execute)
    try:
        result = _run(
            store,
            tmp_path,
            source_database=Path("/homeassistant/other.db"),
        )
    finally:
        store.__exit__(None, None, None)
    assert called is False
    assert result.processed is not None
    assert result.processed.work.status == "blocked"


def test_normal_process_does_not_recover_interrupted_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, DATABASE)
    claimed = database_sync_work.claim_database_sync_work(store)
    assert claimed is not None and claimed.status == "running"
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)
    assert result.processed is None


def test_execution_error_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, DATABASE)
    secret = "github-secret"

    def fail(*args: object, **kwargs: object) -> DatabaseSyncWorkResult:
        raise database_sync_work.DatabaseSyncWorkError(secret)

    monkeypatch.setattr(database_sync_process, "execute_claimed_database_sync_work", fail)
    try:
        with pytest.raises(database_sync_process.DatabaseSyncProcessError) as caught:
            _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)
    assert secret not in str(caught.value)
