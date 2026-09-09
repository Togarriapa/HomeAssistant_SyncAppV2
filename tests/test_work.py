import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.state import StateError, StateStore, WorkItem

NOW = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)


def test_enqueue_is_idempotent_and_claim_is_durable(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        first = store.enqueue_work("candidate", "abc123", now=NOW)
        second = store.enqueue_work("candidate", "abc123", now=NOW + timedelta(seconds=1))
        assert first == second
        claimed = store.claim_work(now=NOW)
        assert claimed is not None
        assert claimed.work_kind == "candidate"
        assert claimed.work_key == "abc123"
        assert claimed.status == "running"
        assert claimed.attempts == 1
    with StateStore(tmp_path) as store:
        recovered = store.recover_interrupted_work(now=NOW + timedelta(minutes=1))
        assert recovered == 1
        retry = store.claim_work(now=NOW + timedelta(minutes=1))
        assert retry is not None
        assert retry.work_key == "abc123"
        assert retry.attempts == 2


def test_transient_failure_uses_bounded_backoff(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("git_push", "main:abc123", now=NOW)
        claimed = store.claim_work(now=NOW)
        assert claimed is not None
        retry = store.fail_work(claimed, transient=True, now=NOW)
        assert retry.status == "retry"
        assert retry.next_attempt_at == NOW + timedelta(seconds=60)
        assert store.claim_work(now=NOW + timedelta(seconds=59)) is None
        claimed_again = store.claim_work(now=NOW + timedelta(seconds=60))
        assert claimed_again is not None
        retry_again = store.fail_work(
            claimed_again, transient=True, now=NOW + timedelta(seconds=60)
        )
        assert retry_again.next_attempt_at == NOW + timedelta(seconds=180)


def test_repeated_transient_failure_eventually_blocks(tmp_path: Path) -> None:
    now = NOW
    with StateStore(tmp_path) as store:
        store.enqueue_work("runtime", "snapshot", now=now)
        item: WorkItem | None = None
        for _ in range(store.MAX_WORK_ATTEMPTS):
            item = store.claim_work(now=now)
            assert item is not None
            item = store.fail_work(item, transient=True, now=now)
            if item.status == "retry":
                assert item.next_attempt_at is not None
                now = item.next_attempt_at
        assert item is not None
        assert item.status == "blocked"
        assert item.next_attempt_at is None
        assert store.claim_work(now=now + timedelta(days=1)) is None


def test_permanent_failure_blocks_and_success_is_not_reexecuted(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate", "badsha", now=NOW)
        bad = store.claim_work(now=NOW)
        assert bad is not None
        blocked = store.fail_work(bad, transient=False, now=NOW)
        assert blocked.status == "blocked"
        assert store.enqueue_work("candidate", "badsha", now=NOW) == blocked
        assert store.claim_work(now=NOW + timedelta(days=1)) is None

        store.enqueue_work("candidate", "goodsha", now=NOW)
        good = store.claim_work(now=NOW)
        assert good is not None
        done = store.complete_work(good, now=NOW)
        assert done.status == "succeeded"
        assert store.enqueue_work("candidate", "goodsha", now=NOW) == done
        assert store.claim_work(now=NOW + timedelta(days=1)) is None


def test_invalid_work_identity_and_stale_transition_fail_closed(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        invalid = (
            ("", "x"),
            ("UPPER", "x"),
            ("candidate", ""),
            ("candidate", "x\nsecret"),
        )
        for kind, key in invalid:
            with pytest.raises(StateError):
                store.enqueue_work(kind, key, now=NOW)
        store.enqueue_work("candidate", "sha", now=NOW)
        claimed = store.claim_work(now=NOW)
        assert claimed is not None
        store.complete_work(claimed, now=NOW)
        with pytest.raises(StateError):
            store.fail_work(claimed, transient=True, now=NOW)


def test_corrupt_work_record_fails_closed(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate", "corrupt", now=NOW)
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE work SET created_at = 'not-a-timestamp' "
            "WHERE work_kind = 'candidate' AND work_key = 'corrupt'"
        )
    with StateStore(tmp_path) as store, pytest.raises(StateError):
        store.claim_work(now=NOW)


def test_schema_v1_is_migrated_without_resetting_identity(tmp_path: Path) -> None:
    root = tmp_path / "syncapp"
    root.mkdir()
    path = root / "state.sqlite3"
    installation_id = "11111111-1111-1111-1111-111111111111"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE installation ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
            "installation_id TEXT NOT NULL, boot_count INTEGER NOT NULL CHECK (boot_count >= 0), "
            "active_run_id TEXT, last_started_at TEXT, last_stopped_at TEXT)"
        )
        db.execute(
            "INSERT INTO installation VALUES (1, ?, 7, NULL, NULL, NULL)",
            (installation_id,),
        )
        db.execute("PRAGMA user_version = 1")
    with StateStore(tmp_path) as store:
        boot = store.start_run()
        assert boot.installation_id == installation_id
        assert boot.boot_count == 8
        queued = store.enqueue_work("local_sync", "stable-change", now=NOW)
        assert queued.status == "pending"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        stored_id = db.execute("SELECT installation_id FROM installation").fetchone()[0]
        assert stored_id == installation_id
