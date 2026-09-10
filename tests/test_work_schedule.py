from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.state import StateStore
from ha_syncapp.work_schedule import RoutineWorkScheduleError, schedule_routine_work


def _claim(store: StateStore, kind: str):
    claimed = store.claim_work_kind(kind)
    assert claimed is not None
    return claimed


def test_missing_work_is_created_as_fresh_pending_generation(tmp_path: Path) -> None:
    scheduled_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    with StateStore(tmp_path) as store:
        item = schedule_routine_work(store, "local_sync", "work", now=scheduled_at)

    assert item.status == "pending"
    assert item.attempts == 0
    assert item.created_at == scheduled_at
    assert item.updated_at == scheduled_at
    assert item.next_attempt_at == scheduled_at


def test_succeeded_work_is_rearmed_as_new_generation(tmp_path: Path) -> None:
    first_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=5)
    with StateStore(tmp_path) as store:
        schedule_routine_work(store, "local_sync", "work", now=first_at)
        completed = store.complete_work(_claim(store, "local_sync"), now=first_at)
        assert completed.status == "succeeded"

        item = schedule_routine_work(store, "local_sync", "work", now=second_at)

    assert item.status == "pending"
    assert item.attempts == 0
    assert item.created_at == second_at
    assert item.updated_at == second_at
    assert item.next_attempt_at == second_at


@pytest.mark.parametrize("status", ["pending", "running", "retry"])
def test_existing_active_or_retry_work_is_coalesced_without_rearming(
    tmp_path: Path, status: str
) -> None:
    first_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    later = first_at + timedelta(minutes=5)
    with StateStore(tmp_path) as store:
        original = schedule_routine_work(store, "local_sync", "work", now=first_at)
        if status in {"running", "retry"}:
            original = _claim(store, "local_sync")
        if status == "retry":
            original = store.fail_work(original, transient=True, now=first_at)
        assert original.status == status

        repeated = schedule_routine_work(store, "local_sync", "work", now=later)

    assert repeated == original


def test_blocked_work_remains_blocked_under_routine_signal(tmp_path: Path) -> None:
    first_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    later = first_at + timedelta(minutes=5)
    with StateStore(tmp_path) as store:
        schedule_routine_work(store, "candidate", "bad-commit", now=first_at)
        blocked = store.fail_work(_claim(store, "candidate"), transient=False, now=first_at)
        assert blocked.status == "blocked"

        repeated = schedule_routine_work(store, "candidate", "bad-commit", now=later)

    assert repeated == blocked


def test_naive_timestamp_fails_closed(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store, pytest.raises(RoutineWorkScheduleError):
        schedule_routine_work(store, "local_sync", "work", now=datetime(2026, 9, 10, 6, 0))


def test_invalid_identity_does_not_disclose_input(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store, pytest.raises(RoutineWorkScheduleError) as error:
        schedule_routine_work(store, "invalid kind", "secret-sentinel")
    assert "secret-sentinel" not in str(error.value)
