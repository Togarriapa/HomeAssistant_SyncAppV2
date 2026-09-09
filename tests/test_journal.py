from pathlib import Path

import pytest
from ha_syncapp.errors import Failure
from ha_syncapp.journal import Journal
from ha_syncapp.state import StateStore


def test_journal_records_audit_trail_and_bounded_retrigger_scan(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        job = journal.enqueue("sync", "snapshot", {})
        journal.claim(now=1)
        journal.checkpoint(job.id, "planned", {"commit": "a" * 40})
        journal.fail(job.id, Failure("network_timeout", retryable=True), now=1)
        assert [e["event"] for e in journal.events()] == [
            "enqueued",
            "attempt_started",
            "checkpoint",
            "attempt_failed",
        ]
        assert "commit" not in str(journal.events())


def test_deduplication_retry_backoff_and_explicit_retry(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        job = journal.enqueue("sync", "tree-one", {"sha": "test"})
        assert journal.enqueue("sync", "tree-one", {}).id == job.id
        assert journal.claim(100).attempts == 1
        journal.fail(job.id, Failure("network_unavailable", retryable=True), now=100)
        assert journal.claim(129) is None
        retry = journal.claim(130)
        assert retry.id == job.id and retry.attempts == 2
        journal.fail(job.id, Failure("invalid_candidate"), now=130)
        assert journal.claim(999999) is None
        assert journal.enqueue("sync", "tree-one", {}).status == "blocked"
        journal.retry(job.id, now=150)
        assert journal.claim(150).attempts == 1
        assert journal.get(job.id).payload["explicit_retry"] is True


def test_restart_preserves_phase_and_payload(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        job = journal.enqueue("candidate", "abc", {})
        journal.claim(100)
        journal.checkpoint(job.id, "applying", {"backup_id": "backup-one"})
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        assert journal.recover() == 1
        recovered = journal.claim(200)
        assert recovered.id == job.id
        assert recovered.phase == "applying"
        assert recovered.payload["backup_id"] == "backup-one"


def test_retry_exhaustion_and_completed_job_are_terminal(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        job = journal.enqueue("runtime", "slot-one", {})
        for count in range(8):
            journal.claim(count * 10000)
            journal.fail(job.id, Failure("timeout", retryable=True), now=count * 10000)
        assert journal.get(job.id).status == "blocked"
        assert journal.claim(999999) is None
        second = journal.enqueue("runtime", "slot-two", {})
        journal.claim(999999)
        journal.succeed(second.id)
        assert journal.claim(999999) is None
        with pytest.raises(Failure):
            journal.retry(second.id)
