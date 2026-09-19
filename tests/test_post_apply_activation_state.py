import sqlite3
from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore


def test_current_schema_contains_post_apply_activation_table(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 13
    with StateStore(tmp_path):
        pass
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        columns = tuple(
            row[1] for row in db.execute("PRAGMA table_info(post_apply_activation_authorization)")
        )
        assert columns == (
            "deployment_id",
            "target",
            "repository_id",
            "baseline_sha",
            "candidate_sha",
            "stage_manifest_sha256",
            "backup_slug",
            "intent_record_sha256",
            "operations_sha256",
            "operation_count",
            "action",
            "authorized_at",
            "record_sha256",
        )
        assert db.execute("PRAGMA user_version").fetchone()[0] == 13


def test_schema_v10_migrates_activation_table_without_losing_work(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("deployment", "preserve-me")
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE post_apply_activation_authorization")
        db.execute("PRAGMA user_version = 10")

    with StateStore(tmp_path) as store:
        item = store.claim_work_kind("deployment")
        assert item is not None
        assert item.work_key == "preserve-me"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 13
