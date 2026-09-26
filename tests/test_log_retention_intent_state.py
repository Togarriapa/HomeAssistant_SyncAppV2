from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
NOW = datetime(2026, 9, 13, 16, 30, tzinfo=UTC)
EXPECTED = "1" * 40
REPLACEMENT = "2" * 40
SNAPSHOT = "f" * 64
WORK_KEY = f"{EXPECTED}:" + "a" * 64


def test_log_retention_intent_is_immutable_and_survives_restart(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with StateStore(data) as store:
        store.bind_repository(TARGET, 123)
        store.record_synchronization_baseline(
            TARGET, "logs", SNAPSHOT, EXPECTED, synchronized_at=NOW
        )
        store.enqueue_work("logs_retention", WORK_KEY, now=NOW)
        item = store.claim_work_kind("logs_retention", now=NOW)
        assert item is not None
        first = store.record_log_retention_intent(
            item,
            TARGET,
            123,
            EXPECTED,
            REPLACEMENT,
            SNAPSHOT,
            recorded_at=NOW,
        )
        repeated = store.record_log_retention_intent(
            item,
            TARGET,
            123,
            EXPECTED,
            REPLACEMENT,
            SNAPSHOT,
            recorded_at=NOW,
        )
        assert first == repeated

    with StateStore(data) as reopened:
        intent = reopened.log_retention_intent(WORK_KEY)

    assert intent is not None
    assert intent.work_key == WORK_KEY
    assert intent.target == TARGET
    assert intent.repository_id == 123
    assert intent.expected_head_sha == EXPECTED
    assert intent.replacement_head_sha == REPLACEMENT
    assert intent.snapshot_id == SNAPSHOT
    assert intent.recorded_at == NOW


def test_log_retention_intent_cannot_be_rebound(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with StateStore(data) as store:
        store.bind_repository(TARGET, 123)
        store.record_synchronization_baseline(
            TARGET, "logs", SNAPSHOT, EXPECTED, synchronized_at=NOW
        )
        store.enqueue_work("logs_retention", WORK_KEY, now=NOW)
        item = store.claim_work_kind("logs_retention", now=NOW)
        assert item is not None
        store.record_log_retention_intent(
            item,
            TARGET,
            123,
            EXPECTED,
            REPLACEMENT,
            SNAPSHOT,
            recorded_at=NOW,
        )

        try:
            store.record_log_retention_intent(
                item,
                TARGET,
                123,
                EXPECTED,
                "3" * 40,
                SNAPSHOT,
                recorded_at=NOW,
            )
        except Exception as error:
            assert type(error).__name__ == "StateError"
        else:
            raise AssertionError("logs retention intent must be immutable")


def test_schema_v6_migrates_log_retention_intent_without_losing_existing_state(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with StateStore(data) as store:
        store.bind_repository(TARGET, 123)
        store.record_synchronization_baseline(
            TARGET, "logs", SNAPSHOT, EXPECTED, synchronized_at=NOW
        )
        queued = store.enqueue_work("logs_retention", WORK_KEY, now=NOW)

    path = data / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE log_retention_intent")
        db.execute("DROP TABLE live_apply_progress")
        db.execute("DROP TABLE live_apply_intent")
        db.execute("PRAGMA user_version = 6")

    with StateStore(data) as migrated:
        claimed = migrated.claim_work_kind("logs_retention", now=NOW)
        assert claimed is not None
        assert claimed.work_key == queued.work_key
        assert migrated.log_retention_intent(WORK_KEY) is None
        assert migrated.repository_id(TARGET) == 123
        baseline = migrated.synchronization_baseline(TARGET, "logs")
        assert baseline is not None
        assert baseline.snapshot_id == SNAPSHOT
        assert baseline.commit_sha == EXPECTED

    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 28
