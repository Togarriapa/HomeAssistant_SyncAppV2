from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.local_sync_schedule import LocalSyncScheduleError, schedule_local_sync_generation
from ha_syncapp.local_sync_work import local_sync_work_key
from ha_syncapp.state import StateStore


def test_local_schedule_uses_existing_deterministic_identity(tmp_path: Path) -> None:
    scheduled_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    with StateStore(tmp_path) as store:
        item = schedule_local_sync_generation(
            store,
            "Owner/Home",
            branch="main",
            now=scheduled_at,
        )

    assert item.work_kind == "local_sync"
    assert item.work_key == local_sync_work_key("Owner/Home", "main")
    assert item.status == "pending"
    assert item.created_at == scheduled_at


def test_local_schedule_rearms_only_after_success(tmp_path: Path) -> None:
    first_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=5)
    with StateStore(tmp_path) as store:
        first = schedule_local_sync_generation(store, "Owner/Home", now=first_at)
        running = store.claim_work_kind("local_sync", now=first_at)
        assert running is not None
        completed = store.complete_work(running, now=first_at)
        assert completed.status == "succeeded"

        second = schedule_local_sync_generation(store, "Owner/Home", now=second_at)

    assert second.work_key == first.work_key
    assert second.status == "pending"
    assert second.attempts == 0
    assert second.created_at == second_at


def test_invalid_target_fails_closed_without_disclosure(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store, pytest.raises(LocalSyncScheduleError) as error:
        schedule_local_sync_generation(store, "secret-sentinel\ninvalid")
    assert "secret-sentinel" not in str(error.value)
