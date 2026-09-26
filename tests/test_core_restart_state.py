import sqlite3
from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore


def test_current_schema_contains_core_restart_attempt_table(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 25
    with StateStore(tmp_path):
        pass
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        columns = tuple(row[1] for row in db.execute("PRAGMA table_info(core_restart_attempt)"))
        assert columns == (
            "deployment_id",
            "authorization_record_sha256",
            "phase",
            "started_at",
            "updated_at",
            "record_sha256",
        )
        assert db.execute("PRAGMA user_version").fetchone()[0] == 25


def test_schema_v11_migrates_restart_table_without_losing_work(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("deployment", "preserve-me")
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE core_restart_attempt")
        db.execute("PRAGMA user_version = 11")

    with StateStore(tmp_path) as store:
        item = store.claim_work_kind("deployment")
        assert item is not None
        assert item.work_key == "preserve-me"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 25
