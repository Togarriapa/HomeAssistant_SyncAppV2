import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
from ha_syncapp.database_snapshot import (
    DatabaseSnapshotError,
    capture_sqlite_snapshot,
)


def _source_and_staging(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "homeassistant"
    staging = tmp_path / "staging"
    source_root.mkdir()
    staging.mkdir()
    return source_root / "home-assistant_v2.db", staging


def _create_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("CREATE TABLE states (entity_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
    connection.commit()
    return connection


def test_snapshot_contains_committed_uncheckpointed_wal_state(tmp_path: Path) -> None:
    source, staging = _source_and_staging(tmp_path)
    writer = _create_database(source)
    try:
        writer.execute("INSERT INTO states VALUES ('light.kitchen', 'on')")
        writer.commit()
        wal = Path(f"{source}-wal")
        assert wal.exists() and wal.stat().st_size > 0

        snapshot = capture_sqlite_snapshot(source, staging)

        backup = sqlite3.connect(f"file:{snapshot.database_path}?mode=ro", uri=True)
        try:
            row = backup.execute(
                "SELECT entity_id, state FROM states WHERE entity_id = 'light.kitchen'"
            ).fetchone()
            integrity = backup.execute("PRAGMA quick_check").fetchall()
        finally:
            backup.close()
    finally:
        writer.close()

    assert row == ("light.kitchen", "on")
    assert integrity == [("ok",)]
    payload = snapshot.database_path.read_bytes()
    assert snapshot.size == len(payload)
    assert snapshot.sha256 == hashlib.sha256(payload).hexdigest()
    assert snapshot.root.parent == staging.resolve()


def test_snapshot_does_not_modify_source_database_bytes(tmp_path: Path) -> None:
    source, staging = _source_and_staging(tmp_path)
    writer = _create_database(source)
    try:
        writer.execute("INSERT INTO states VALUES ('sensor.temp', '20')")
        writer.commit()
        database_before = source.read_bytes()
        wal_path = Path(f"{source}-wal")
        wal_before = wal_path.read_bytes()

        capture_sqlite_snapshot(source, staging)

        assert source.read_bytes() == database_before
        assert wal_path.read_bytes() == wal_before
    finally:
        writer.close()


def test_symlinked_source_is_rejected_without_following_it(tmp_path: Path) -> None:
    source, staging = _source_and_staging(tmp_path)
    real = source.with_name("real.db")
    connection = sqlite3.connect(real)
    connection.execute("CREATE TABLE test (id INTEGER)")
    connection.commit()
    connection.close()
    source.symlink_to(real)

    with pytest.raises(DatabaseSnapshotError, match="safe regular file"):
        capture_sqlite_snapshot(source, staging)

    assert list(staging.iterdir()) == []


def test_hardlinked_source_is_rejected(tmp_path: Path) -> None:
    source, staging = _source_and_staging(tmp_path)
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE test (id INTEGER)")
    connection.commit()
    connection.close()
    os.link(source, source.with_name("second-link.db"))

    with pytest.raises(DatabaseSnapshotError, match="unique regular file"):
        capture_sqlite_snapshot(source, staging)

    assert list(staging.iterdir()) == []


def test_non_database_fails_closed_and_cleans_incomplete_stage(tmp_path: Path) -> None:
    source, staging = _source_and_staging(tmp_path)
    source.write_bytes(b"not a sqlite database")

    with pytest.raises(DatabaseSnapshotError, match="SQLite backup failed closed") as error:
        capture_sqlite_snapshot(source, staging)

    assert "not a database" not in str(error.value).lower()
    assert list(staging.iterdir()) == []


def test_staging_inside_source_tree_is_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "homeassistant"
    source_root.mkdir()
    source = source_root / "home-assistant_v2.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE test (id INTEGER)")
    connection.commit()
    connection.close()
    staging = source_root / "snapshots"
    staging.mkdir()

    with pytest.raises(DatabaseSnapshotError, match="overlaps"):
        capture_sqlite_snapshot(source, staging)

    assert list(staging.iterdir()) == []


def test_symlinked_staging_root_is_rejected(tmp_path: Path) -> None:
    source_root = tmp_path / "homeassistant"
    source_root.mkdir()
    source = source_root / "home-assistant_v2.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE test (id INTEGER)")
    connection.commit()
    connection.close()
    real_staging = tmp_path / "real-staging"
    real_staging.mkdir()
    alias = tmp_path / "staging"
    alias.symlink_to(real_staging, target_is_directory=True)

    with pytest.raises(DatabaseSnapshotError, match="real directory"):
        capture_sqlite_snapshot(source, alias)

    assert list(real_staging.iterdir()) == []
