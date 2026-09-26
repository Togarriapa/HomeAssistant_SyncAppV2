import sqlite3
from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore


def test_schema_v16_contains_integration_observation_table(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 26
    with StateStore(tmp_path):
        pass
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        columns = tuple(row[1] for row in db.execute("PRAGMA table_info(integration_observation)"))
        assert columns == (
            "deployment_id",
            "supervisor_health_sha256",
            "observed_at",
            "entry_count",
            "disabled_count",
            "record_sha256",
        )
        assert db.execute("PRAGMA user_version").fetchone()[0] == 26


def test_schema_v15_migrates_without_losing_work(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("deployment", "preserve-me")
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE integration_observation")
        db.execute("PRAGMA user_version = 15")

    with StateStore(tmp_path) as store:
        assert store.claim_work_kind("deployment").work_key == "preserve-me"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 26
