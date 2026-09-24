from __future__ import annotations

import sqlite3
from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_schema_version_includes_rollback_recovery_authority() -> None:
    assert SCHEMA_VERSION == 25


def test_fresh_state_has_integrity_bound_rollback_recovery_authority_table(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        columns = {
            str(row[1]): str(row[2])
            for row in store._connection.execute(
                "PRAGMA table_info(rollback_recovery_authority)"
            ).fetchall()
        }
        assert columns == {
            "deployment_id": "TEXT",
            "schema_version": "INTEGER",
            "candidate_sha": "TEXT",
            "entity_ids_json": "TEXT",
            "resource_target_sha256": "TEXT",
            "automation_target_sha256": "TEXT",
            "assertion_canonical_json": "TEXT",
            "assertion_set_sha256": "TEXT",
            "record_sha256": "TEXT",
        }
        foreign_keys = store._connection.execute(
            "PRAGMA foreign_key_list(rollback_recovery_authority)"
        ).fetchall()
        assert any(
            str(row[2]) == "deployment_rollback"
            and str(row[3]) == "deployment_id"
            and str(row[4]) == "deployment_id"
            for row in foreign_keys
        )
    finally:
        store.__exit__(None, None, None)


def test_state_connection_enforces_foreign_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        assert store._connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        store.__exit__(None, None, None)


def test_recovery_authority_cannot_exist_without_rollback_intent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        try:
            store._connection.execute(
                "INSERT INTO rollback_recovery_authority VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "orphan-deployment",
                    1,
                    "a" * 40,
                    "[]",
                    "b" * 64,
                    "c" * 64,
                    "[]",
                    "d" * 64,
                    "e" * 64,
                ),
            )
        except Exception as error:
            assert "FOREIGN KEY" in str(error).upper()
        else:
            raise AssertionError("orphan recovery authority must be rejected")
    finally:
        store.__exit__(None, None, None)


def test_schema_24_store_migrates_recovery_authority_transactionally(tmp_path: Path) -> None:
    data = tmp_path / "data"
    root = data / "syncapp"
    root.mkdir(parents=True)
    database = root / "state.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE installation ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
            "installation_id TEXT NOT NULL, boot_count INTEGER NOT NULL CHECK (boot_count >= 0), "
            "active_run_id TEXT, last_started_at TEXT, last_stopped_at TEXT)"
        )
        db.execute(
            "INSERT INTO installation VALUES "
            "(1, '00000000-0000-0000-0000-000000000001', 0, NULL, NULL, NULL)"
        )
        db.execute(
            "CREATE TABLE deployment_rollback ("
            "deployment_id TEXT PRIMARY KEY NOT NULL, target TEXT NOT NULL, "
            "repository_id INTEGER NOT NULL CHECK (repository_id > 0), "
            "baseline_sha TEXT NOT NULL, candidate_sha TEXT NOT NULL, backup_slug TEXT NOT NULL, "
            "finalization_sha256 TEXT NOT NULL, repository_proof_sha256 TEXT NOT NULL, "
            "backup_proof_sha256 TEXT NOT NULL, phase TEXT NOT NULL, "
            "reconciliation_state TEXT NOT NULL, block_reason TEXT NOT NULL, "
            "attempt_count INTEGER NOT NULL, restore_job_id TEXT, authorized_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, record_sha256 TEXT NOT NULL)"
        )
        db.execute("PRAGMA user_version = 24")

    store = StateStore(data)
    store.__enter__()
    try:
        assert store._connection.execute("PRAGMA user_version").fetchone() == (25,)
        assert store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'rollback_recovery_authority'"
        ).fetchone() == ("rollback_recovery_authority",)
    finally:
        store.__exit__(None, None, None)
