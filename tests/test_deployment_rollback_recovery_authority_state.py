from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from ha_syncapp.state import SCHEMA_VERSION, StateError, StateStore


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_schema_version_includes_rollback_recovery_authority() -> None:
    assert SCHEMA_VERSION == 38


def test_state_connection_enforces_foreign_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        assert store._connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        store.__exit__(None, None, None)


def test_open_fails_closed_when_sqlite_cannot_enable_foreign_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_connect = sqlite3.connect

    class ForeignKeysDisabledConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
            if sql == "PRAGMA foreign_keys = ON":
                return super().execute("SELECT 1")
            if sql == "PRAGMA foreign_keys":
                return super().execute("SELECT 0")
            return super().execute(sql, parameters)

    def connect_without_foreign_keys(*args: object, **kwargs: object) -> sqlite3.Connection:
        return original_connect(*args, factory=ForeignKeysDisabledConnection, **kwargs)

    monkeypatch.setattr("ha_syncapp.state.sqlite3.connect", connect_without_foreign_keys)
    data = tmp_path / "data"
    data.mkdir()

    with pytest.raises(StateError, match="foreign keys"):
        StateStore(data).__enter__()


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
