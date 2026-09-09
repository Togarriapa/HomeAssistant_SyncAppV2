from pathlib import Path

import pytest
from ha_syncapp import database_sync_retrigger, database_sync_work
from ha_syncapp.database_sync import DatabaseSyncDisposition, DatabaseSyncResult
from ha_syncapp.database_sync_work import DatabaseSyncWorkResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
COMMIT_SHA = "1" * 40
DB_DIGEST = "d" * 64
SNAPSHOT_ID = "a" * 64


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    store = StateStore(data)
    store.__enter__()
    return store


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "homeassistant" / "home-assistant_v2.db"
    database.parent.mkdir(exist_ok=True)
    database.write_bytes(b"database")
    return database


def _sync_result(disposition: DatabaseSyncDisposition) -> DatabaseSyncResult:
    return DatabaseSyncResult(
        disposition=disposition,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="database",
        database_sha256=DB_DIGEST,
        database_size=8,
        snapshot_id=SNAPSHOT_ID,
        commit_sha=COMMIT_SHA,
        baseline=None,
    )


def _run(
    store: StateStore,
    database: Path,
    tmp_path: Path,
    *,
    token: str = "token",
) -> database_sync_retrigger.DatabaseSyncRetriggerResult:
    return database_sync_retrigger.run_database_sync_retrigger_pass(
        store,
        database,
        tmp_path / "database-staging",
        tmp_path / "snapshots",
        tmp_path / "workspaces",
        TARGET,
        token,
    )


def test_retrigger_pass_returns_no_work_without_consuming_other_kinds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    store.enqueue_work("candidate", "candidate-sha")
    try:
        result = _run(store, database, tmp_path)
        unrelated = store.claim_work()
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 0
    assert result.processed is None
    assert unrelated is not None and unrelated.work_kind == "candidate"


def test_retrigger_pass_recovers_interrupted_database_work_before_claiming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, database)
    claimed = database_sync_work.claim_database_sync_work(store)
    assert claimed is not None and claimed.status == "running"

    def succeed(state: StateStore, item, *args: object) -> DatabaseSyncWorkResult:
        assert item.status == "running"
        assert item.attempts == 2
        completed = state.complete_work(item)
        return DatabaseSyncWorkResult(
            completed,
            _sync_result(DatabaseSyncDisposition.NO_CHANGE),
        )

    monkeypatch.setattr(
        database_sync_retrigger,
        "execute_claimed_database_sync_work",
        succeed,
    )
    try:
        result = _run(store, database, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 1
    assert result.processed is not None
    assert result.processed.work.status == "succeeded"
    assert result.processed.work.attempts == 2


def test_retrigger_pass_processes_at_most_one_database_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _database(tmp_path)
    second = tmp_path / "homeassistant" / "second.db"
    second.write_bytes(b"second")
    database_sync_work.enqueue_database_sync_work(store, TARGET, first)
    database_sync_work.enqueue_database_sync_work(store, TARGET, second)

    def succeed(state: StateStore, item, *args: object) -> DatabaseSyncWorkResult:
        completed = state.complete_work(item)
        return DatabaseSyncWorkResult(
            completed,
            _sync_result(DatabaseSyncDisposition.NO_CHANGE),
        )

    monkeypatch.setattr(
        database_sync_retrigger,
        "execute_claimed_database_sync_work",
        succeed,
    )
    try:
        result = _run(store, first, tmp_path)
        remaining = database_sync_work.claim_database_sync_work(store)
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert remaining is not None


def test_retrigger_pass_preserves_deterministic_block_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, database)
    monkeypatch.setattr(
        database_sync_work,
        "synchronize_database_snapshot",
        lambda *args, **kwargs: _sync_result(DatabaseSyncDisposition.DIVERGED),
    )
    try:
        result = _run(store, database, tmp_path)
        assert database_sync_work.claim_database_sync_work(store) is None
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == "blocked"
    assert result.processed.work.next_attempt_at is None


def test_retrigger_pass_preserves_transient_retry_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, database)

    def fail(*args: object, **kwargs: object) -> DatabaseSyncResult:
        from ha_syncapp.database_sync import DatabaseSyncError

        raise DatabaseSyncError("sanitized")

    monkeypatch.setattr(database_sync_work, "synchronize_database_snapshot", fail)
    try:
        result = _run(store, database, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == "retry"
    assert result.processed.work.next_attempt_at is not None


def test_retrigger_pass_does_not_expose_token_in_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, database)
    secret = "ghp_super_secret_value"

    def fail(*args: object, **kwargs: object) -> DatabaseSyncWorkResult:
        raise database_sync_work.DatabaseSyncWorkError(secret)

    monkeypatch.setattr(
        database_sync_retrigger,
        "execute_claimed_database_sync_work",
        fail,
    )
    try:
        with pytest.raises(database_sync_retrigger.DatabaseSyncRetriggerError) as error:
            _run(store, database, tmp_path, token=secret)
    finally:
        store.__exit__(None, None, None)

    assert secret not in str(error.value)


def test_retrigger_pass_fails_closed_for_unopened_state(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    database = _database(tmp_path)

    with pytest.raises(
        database_sync_retrigger.DatabaseSyncRetriggerError,
        match="failed closed",
    ):
        _run(store, database, tmp_path)
