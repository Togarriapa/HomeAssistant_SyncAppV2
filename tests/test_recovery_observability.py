"""TDD contract for bounded, sanitized Retrigger recovery observability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ha_syncapp.recovery_observability import build_recovery_status
from ha_syncapp.state import StateStore


def _open_store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_recovery_status_exposes_each_operation_without_raw_work_key(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    reference = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
    sensitive_key = "/homeassistant/secrets.yaml"
    try:
        pending = store.enqueue_work("local_sync", sensitive_key, now=reference)
        running = store.claim_work_kind("local_sync", now=reference)
        assert running is not None and running.work_key == pending.work_key
        store.fail_work(running, transient=True, now=reference)

        blocked_seed = store.enqueue_work("candidate", "a" * 40, now=reference)
        blocked_running = store.claim_work_kind("candidate", now=reference)
        assert blocked_running is not None and blocked_running.work_key == blocked_seed.work_key
        store.fail_work(blocked_running, transient=False, now=reference)

        status = build_recovery_status(store, reference_time=reference)
    finally:
        store.__exit__(None, None, None)

    assert status["summary"] == {
        "total": 2,
        "pending": 0,
        "running": 0,
        "retry": 1,
        "blocked": 1,
        "succeeded": 0,
    }
    operations = status["operations"]
    assert len(operations) == 2
    assert {item["work_kind"] for item in operations} == {"candidate", "local_sync"}
    assert {item["status"] for item in operations} == {"blocked", "retry"}
    assert all(item["attempts"] == 1 for item in operations)
    assert all(len(item["operation_id"]) == 32 for item in operations)
    rendered = repr(status)
    assert sensitive_key not in rendered
    assert "a" * 40 not in rendered


def test_recovery_status_reports_retry_due_state_from_explicit_reference_time(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    started = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
    try:
        store.enqueue_work("logs", "artifact-1", now=started)
        running = store.claim_work_kind("logs", now=started)
        assert running is not None
        retry = store.fail_work(running, transient=True, now=started)
        assert retry.next_attempt_at == started + timedelta(seconds=60)

        before = build_recovery_status(store, reference_time=started + timedelta(seconds=59))
        due = build_recovery_status(store, reference_time=started + timedelta(seconds=60))
    finally:
        store.__exit__(None, None, None)

    assert before["operations"][0]["retry_state"] == "deferred"
    assert due["operations"][0]["retry_state"] == "due"
    assert before["generated_at"] == "2026-09-13T01:00:59+00:00"
    assert due["generated_at"] == "2026-09-13T01:01:00+00:00"


def test_recovery_status_is_deterministic_and_empty_safe(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    reference = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
    try:
        first = build_recovery_status(store, reference_time=reference)
        second = build_recovery_status(store, reference_time=reference)
    finally:
        store.__exit__(None, None, None)

    assert first == second
    assert first == {
        "generated_at": "2026-09-13T01:00:00+00:00",
        "summary": {
            "total": 0,
            "pending": 0,
            "running": 0,
            "retry": 0,
            "blocked": 0,
            "succeeded": 0,
        },
        "operations": [],
    }
