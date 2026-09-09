from pathlib import Path

import pytest
from ha_syncapp import database_sync_work
from ha_syncapp.database_sync import DatabaseSyncDisposition, DatabaseSyncError, DatabaseSyncResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
COMMIT_SHA = "1" * 40
DB_DIGEST = "d" * 64
SNAPSHOT_ID = "a" * 64


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _database(tmp_path: Path) -> Path:
    source = tmp_path / "homeassistant" / "home-assistant_v2.db"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b"database")
    return source


def _result(disposition: DatabaseSyncDisposition) -> DatabaseSyncResult:
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


def _execute(
    store: StateStore,
    item,
    database: Path,
    tmp_path: Path,
) -> database_sync_work.DatabaseSyncWorkResult:
    return database_sync_work.execute_claimed_database_sync_work(
        store,
        item,
        database,
        tmp_path / "database-staging",
        tmp_path / "snapshots",
        tmp_path / "workspaces",
        TARGET,
        "secret-token",
    )


def test_enqueue_database_work_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    try:
        first = database_sync_work.enqueue_database_sync_work(store, TARGET, database)
        second = database_sync_work.enqueue_database_sync_work(store, TARGET, database)
    finally:
        store.__exit__(None, None, None)

    assert first.work_kind == "database"
    assert second.work_key == first.work_key
    assert second.status == "pending"


def test_database_claim_does_not_consume_other_work_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    store.enqueue_work("candidate", "candidate-a")
    database_sync_work.enqueue_database_sync_work(store, TARGET, database)
    try:
        claimed = database_sync_work.claim_database_sync_work(store)
        unrelated = store.claim_work()
    finally:
        store.__exit__(None, None, None)

    assert claimed is not None and claimed.work_kind == "database"
    assert unrelated is not None and unrelated.work_kind == "candidate"


@pytest.mark.parametrize(
    "disposition",
    [
        DatabaseSyncDisposition.INITIALIZED,
        DatabaseSyncDisposition.PUBLISHED,
        DatabaseSyncDisposition.NO_CHANGE,
    ],
)
def test_successful_database_outcomes_complete_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: DatabaseSyncDisposition,
) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, database)
    item = database_sync_work.claim_database_sync_work(store)
    assert item is not None
    monkeypatch.setattr(database_sync_work, "synchronize_database_snapshot", lambda *a, **k: _result(disposition))
    try:
        result = _execute(store, item, database, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "succeeded"
    assert result.synchronization is not None
    assert result.synchronization.disposition is disposition


@pytest.mark.parametrize(
    "disposition",
    [
        DatabaseSyncDisposition.BASELINE_REQUIRED,
        DatabaseSyncDisposition.DIVERGED,
        DatabaseSyncDisposition.REMOTE_MISSING,
    ],
)
def test_deterministic_database_refusals_block_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: DatabaseSyncDisposition,
) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, database)
    item = database_sync_work.claim_database_sync_work(store)
    assert item is not None
    monkeypatch.setattr(database_sync_work, "synchronize_database_snapshot", lambda *a, **k: _result(disposition))
    try:
        result = _execute(store, item, database, tmp_path)
        assert database_sync_work.claim_database_sync_work(store) is None
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "blocked"
    assert result.work.next_attempt_at is None


def test_guarded_database_failure_uses_retry_without_leaking_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    database_sync_work.enqueue_database_sync_work(store, TARGET, database)
    item = database_sync_work.claim_database_sync_work(store)
    assert item is not None
    secret = "ghp_super_secret_value"

    def fail(*args: object, **kwargs: object) -> DatabaseSyncResult:
        raise DatabaseSyncError(secret)

    monkeypatch.setattr(database_sync_work, "synchronize_database_snapshot", fail)
    try:
        result = database_sync_work.execute_claimed_database_sync_work(
            store,
            item,
            database,
            tmp_path / "database-staging",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            secret,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "retry"
    assert result.synchronization is None
    assert secret not in repr(result)


def test_claim_identity_must_match_explicit_database_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    database = _database(tmp_path)
    other = tmp_path / "homeassistant" / "other.db"
    other.write_bytes(b"other")
    database_sync_work.enqueue_database_sync_work(store, TARGET, database)
    item = database_sync_work.claim_database_sync_work(store)
    assert item is not None
    try:
        with pytest.raises(
            database_sync_work.DatabaseSyncWorkError,
            match="identity does not match",
        ):
            _execute(store, item, other, tmp_path)
    finally:
        store.__exit__(None, None, None)


def test_work_key_rejects_relative_database_path() -> None:
    with pytest.raises(database_sync_work.DatabaseSyncWorkError, match="source path is invalid"):
        database_sync_work.database_sync_work_key(TARGET, Path("home-assistant_v2.db"))
