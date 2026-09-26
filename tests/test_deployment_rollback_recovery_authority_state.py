from __future__ import annotations

from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_schema_version_includes_rollback_recovery_authority() -> None:
    assert SCHEMA_VERSION == 28


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
        store._connection.execute("PRAGMA foreign_keys = ON")
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
