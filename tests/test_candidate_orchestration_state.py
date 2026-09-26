from __future__ import annotations

from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore


def test_schema_version_includes_candidate_orchestration() -> None:
    assert SCHEMA_VERSION == 26


def test_fresh_state_has_candidate_orchestration_table(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        columns = {
            str(row[1]): str(row[2])
            for row in store._connection.execute(
                "PRAGMA table_info(candidate_orchestration)"
            ).fetchall()
        }
        assert columns == {
            "candidate_sha": "TEXT",
            "schema_version": "INTEGER",
            "target": "TEXT",
            "repository_id": "INTEGER",
            "phase": "TEXT",
            "next_action": "TEXT",
            "registered_at": "TEXT",
            "updated_at": "TEXT",
            "record_sha256": "TEXT",
        }
        foreign_keys = store._connection.execute(
            "PRAGMA foreign_key_list(candidate_orchestration)"
        ).fetchall()
        assert any(
            str(row[2]) == "repository_binding"
            and str(row[3]) == "target"
            and str(row[4]) == "target"
            for row in foreign_keys
        )
        assert any(
            str(row[2]) == "work"
            and str(row[3]) == "candidate_sha"
            and str(row[4]) == "work_key"
            for row in foreign_keys
        )
