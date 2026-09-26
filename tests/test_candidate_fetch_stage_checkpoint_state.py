from __future__ import annotations

from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore


def test_schema_version_includes_candidate_fetch_stage_checkpoint() -> None:
    assert SCHEMA_VERSION == 27


def test_fresh_state_has_candidate_fetch_stage_checkpoint_table(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        columns = {
            str(row[1]): str(row[2])
            for row in store._connection.execute(
                "PRAGMA table_info(candidate_fetch_stage_checkpoint)"
            ).fetchall()
        }
        assert columns == {
            "candidate_sha": "TEXT",
            "schema_version": "INTEGER",
            "orchestration_sha256": "TEXT",
            "target": "TEXT",
            "repository_id": "INTEGER",
            "workspace_id": "TEXT",
            "phase": "TEXT",
            "manifest_sha256": "TEXT",
            "entry_count": "INTEGER",
            "total_bytes": "INTEGER",
            "planned_at": "TEXT",
            "completed_at": "TEXT",
            "record_sha256": "TEXT",
        }
        foreign_keys = store._connection.execute(
            "PRAGMA foreign_key_list(candidate_fetch_stage_checkpoint)"
        ).fetchall()
        assert any(
            str(row[2]) == "candidate_orchestration"
            and str(row[3]) == "candidate_sha"
            and str(row[4]) == "candidate_sha"
            for row in foreign_keys
        )


def test_schema_26_migrates_checkpoint_and_expands_orchestration(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store._connection.execute("DROP TABLE candidate_fetch_stage_checkpoint")
        store._connection.execute("PRAGMA user_version = 26")

    with StateStore(tmp_path) as migrated:
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (27,)
        sql = migrated._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'candidate_orchestration'"
        ).fetchone()[0]
        assert "'staged'" in sql
        assert "'analyze'" in sql
