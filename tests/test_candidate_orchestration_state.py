from __future__ import annotations

from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore


def test_schema_version_includes_candidate_orchestration() -> None:
    assert SCHEMA_VERSION == 33


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
            "work_kind": "TEXT",
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


def test_fresh_state_has_candidate_dependency_checkpoint_table(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        columns = {
            str(row[1]): str(row[2])
            for row in store._connection.execute(
                "PRAGMA table_info(candidate_dependency_checkpoint)"
            ).fetchall()
        }
        assert columns == {
            "candidate_sha": "TEXT",
            "schema_version": "INTEGER",
            "orchestration_sha256": "TEXT",
            "fetch_stage_sha256": "TEXT",
            "integrity_sha256": "TEXT",
            "target": "TEXT",
            "repository_id": "INTEGER",
            "baseline_sha": "TEXT",
            "stage_manifest_sha256": "TEXT",
            "phase": "TEXT",
            "runtime_json": "TEXT",
            "dependencies_json": "TEXT",
            "reference_count": "INTEGER",
            "planned_at": "TEXT",
            "completed_at": "TEXT",
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
        work_keys = {(str(row[3]), str(row[4])) for row in foreign_keys if str(row[2]) == "work"}
        assert work_keys == {("work_kind", "work_kind"), ("candidate_sha", "work_key")}


def test_fresh_state_has_candidate_risk_checkpoint_table(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        columns = {
            str(row[1])
            for row in store._connection.execute(
                "PRAGMA table_info(candidate_risk_checkpoint)"
            ).fetchall()
        }
        assert {
            "candidate_sha",
            "dependency_sha256",
            "impact_json",
            "risk_json",
            "risk_level",
            "affected_count",
            "record_sha256",
        }.issubset(columns)


def test_fresh_state_has_candidate_static_checkpoint_table(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        columns = {
            str(row[1])
            for row in store._connection.execute(
                "PRAGMA table_info(candidate_static_checkpoint)"
            ).fetchall()
        }
        assert {
            "candidate_sha",
            "risk_sha256",
            "validation_json",
            "syntax_valid",
            "invalid_count",
            "unvalidated_count",
            "record_sha256",
        }.issubset(columns)


def test_fresh_state_has_candidate_semantic_checkpoint_table(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        columns = {
            str(row[1])
            for row in store._connection.execute(
                "PRAGMA table_info(candidate_semantic_checkpoint)"
            ).fetchall()
        }
        assert {
            "candidate_sha",
            "orchestration_sha256",
            "fetch_stage_sha256",
            "integrity_sha256",
            "dependency_sha256",
            "risk_sha256",
            "static_sha256",
            "stage_manifest_sha256",
            "core_version",
            "semantic_json",
            "phase",
            "record_sha256",
        }.issubset(columns)


def test_fresh_state_has_candidate_backup_checkpoint_table(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        columns = {
            str(row[1])
            for row in store._connection.execute(
                "PRAGMA table_info(candidate_backup_checkpoint)"
            ).fetchall()
        }
        assert columns == {
            "candidate_sha",
            "schema_version",
            "orchestration_sha256",
            "fetch_stage_sha256",
            "integrity_sha256",
            "dependency_sha256",
            "risk_sha256",
            "static_sha256",
            "semantic_sha256",
            "target",
            "repository_id",
            "baseline_sha",
            "stage_manifest_sha256",
            "runtime_sha256",
            "risk_level",
            "core_version",
            "deployment_id",
            "request_name",
            "phase",
            "backup_slug",
            "planned_at",
            "started_at",
            "completed_at",
            "record_sha256",
        }


def test_schema_25_migrates_transactionally_to_candidate_orchestration(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        store._connection.execute("DROP TABLE candidate_orchestration")
        store._connection.execute("PRAGMA user_version = 25")

    with StateStore(root) as migrated:
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (33,)
        table = migrated._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'candidate_orchestration'"
        ).fetchone()
        assert table == ("candidate_orchestration",)


def test_schema_28_migrates_transactionally_to_dependency_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        store._connection.execute("DROP TABLE candidate_dependency_checkpoint")
        store._connection.execute("PRAGMA user_version = 28")

    with StateStore(root) as migrated:
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (33,)
        table = migrated._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'candidate_dependency_checkpoint'"
        ).fetchone()
        assert table == ("candidate_dependency_checkpoint",)


def test_schema_31_migrates_transactionally_to_semantic_authority(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        store._connection.execute("DROP TABLE candidate_semantic_checkpoint")
        store._connection.execute("PRAGMA user_version = 31")

    with StateStore(root) as migrated:
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (33,)
        table = migrated._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'candidate_semantic_checkpoint'"
        ).fetchone()
        assert table == ("candidate_semantic_checkpoint",)


def test_schema_32_migrates_transactionally_to_backup_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with StateStore(root) as store:
        store._connection.execute("DROP TABLE candidate_backup_checkpoint")
        store._connection.execute("PRAGMA user_version = 32")

    with StateStore(root) as migrated:
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (33,)
        table = migrated._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'candidate_backup_checkpoint'"
        ).fetchone()
        assert table == ("candidate_backup_checkpoint",)
