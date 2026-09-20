import sqlite3
from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore


def test_schema_v17_contains_startup_error_observation_table(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 22
    with StateStore(tmp_path):
        pass
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        columns = tuple(
            row[1] for row in db.execute("PRAGMA table_info(startup_error_observation)")
        )
        assert columns == (
            "deployment_id",
            "integration_observation_sha256",
            "restart_attempt_sha256",
            "interval_started_at",
            "observed_at",
            "inspected_count",
            "warning_count",
            "significant_error_count",
            "record_sha256",
        )
        assert db.execute("PRAGMA user_version").fetchone()[0] == 22


def test_schema_v16_migrates_without_losing_work(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("deployment", "preserve-me")
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE startup_error_observation")
        db.execute("PRAGMA user_version = 16")

    with StateStore(tmp_path) as store:
        assert store.claim_work_kind("deployment").work_key == "preserve-me"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 22
