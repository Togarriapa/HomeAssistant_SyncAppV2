import sqlite3
from pathlib import Path

from ha_syncapp.state import SCHEMA_VERSION, StateStore

_EXPECTED_COLUMNS = (
    "deployment_id",
    "target",
    "repository_id",
    "baseline_sha",
    "candidate_sha",
    "stage_manifest_sha256",
    "backup_slug",
    "homeassistant_root",
    "operations_sha256",
    "recorded_at",
    "record_sha256",
)


def _columns(path: Path) -> tuple[str, ...]:
    with sqlite3.connect(path) as db:
        return tuple(row[1] for row in db.execute("PRAGMA table_info(live_apply_intent)"))


def test_current_schema_contains_durable_live_apply_intent_table(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 8
    with StateStore(tmp_path):
        pass

    path = tmp_path / "syncapp/state.sqlite3"
    assert _columns(path) == _EXPECTED_COLUMNS
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8


def test_schema_v7_migrates_live_apply_intent_without_losing_work(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("deployment", "preserve-me")

    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE live_apply_intent")
        db.execute("PRAGMA user_version = 7")

    with StateStore(tmp_path) as store:
        item = store.claim_work_kind("deployment")
        assert item is not None
        assert item.work_key == "preserve-me"

    assert _columns(path) == _EXPECTED_COLUMNS
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
