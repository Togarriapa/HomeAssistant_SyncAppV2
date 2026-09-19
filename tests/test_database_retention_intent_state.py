from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
NOW = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
EXPECTED = "1" * 40
REPLACEMENT = "2" * 40
SNAPSHOT = "f" * 64
WORK_KEY = "a" * 64


def test_database_retention_intent_is_immutable_and_survives_restart(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path) as store:
        store.bind_repository(TARGET, 123)
        store.record_synchronization_baseline(
            TARGET, "database", SNAPSHOT, EXPECTED, synchronized_at=NOW
        )
        store.enqueue_work("database_retention", WORK_KEY, now=NOW)
        item = store.claim_work_kind("database_retention", now=NOW)
        assert item is not None
        first = store.record_database_retention_intent(
            item,
            TARGET,
            123,
            EXPECTED,
            REPLACEMENT,
            SNAPSHOT,
            recorded_at=NOW,
        )
        repeated = store.record_database_retention_intent(
            item,
            TARGET,
            123,
            EXPECTED,
            REPLACEMENT,
            SNAPSHOT,
            recorded_at=NOW,
        )
        assert first == repeated

    with StateStore(tmp_path) as reopened:
        intent = reopened.database_retention_intent(WORK_KEY)

    assert intent is not None
    assert intent.work_key == WORK_KEY
    assert intent.target == TARGET
    assert intent.repository_id == 123
    assert intent.expected_head_sha == EXPECTED
    assert intent.replacement_head_sha == REPLACEMENT
    assert intent.snapshot_id == SNAPSHOT
    assert intent.recorded_at == NOW


def test_schema_v5_migrates_retention_intent_without_losing_work(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.bind_repository(TARGET, 123)
        store.record_synchronization_baseline(
            TARGET, "database", SNAPSHOT, EXPECTED, synchronized_at=NOW
        )
        queued = store.enqueue_work("database_retention", WORK_KEY, now=NOW)

    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE database_retention_intent")
        db.execute("DROP TABLE live_apply_progress")
        db.execute("DROP TABLE live_apply_intent")
        db.execute("PRAGMA user_version = 5")

    with StateStore(tmp_path) as migrated:
        claimed = migrated.claim_work_kind("database_retention", now=NOW)
        assert claimed is not None
        assert claimed.work_key == queued.work_key
        assert migrated.database_retention_intent(WORK_KEY) is None

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 12
