from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.database_sync_schedule import (
    DatabaseSyncScheduleError,
    schedule_database_sync_generation,
)
from ha_syncapp.database_sync_work import database_sync_work_key
from ha_syncapp.runtime_sync_schedule import RuntimeSyncScheduleError, schedule_runtime_sync_generation
from ha_syncapp.runtime_sync_work import runtime_sync_work_key
from ha_syncapp.state import StateStore


def test_database_schedule_uses_target_and_absolute_recorder_identity(tmp_path: Path) -> None:
    scheduled_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    database = tmp_path / "home-assistant_v2.db"
    with StateStore(tmp_path / "state") as store:
        item = schedule_database_sync_generation(
            store,
            "Owner/Home",
            database,
            now=scheduled_at,
        )

    assert item.work_kind == "database"
    assert item.work_key == database_sync_work_key("Owner/Home", database)
    assert item.status == "pending"
    assert item.created_at == scheduled_at


def test_database_schedule_rearms_after_success(tmp_path: Path) -> None:
    first_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=5)
    database = tmp_path / "home-assistant_v2.db"
    data = tmp_path / "state"
    data.mkdir()
    with StateStore(data) as store:
        first = schedule_database_sync_generation(store, "Owner/Home", database, now=first_at)
        running = store.claim_work_kind("database", now=first_at)
        assert running is not None
        store.complete_work(running, now=first_at)
        second = schedule_database_sync_generation(store, "Owner/Home", database, now=second_at)

    assert second.work_key == first.work_key
    assert second.status == "pending"
    assert second.attempts == 0
    assert second.created_at == second_at


def test_database_schedule_rejects_relative_source(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store, pytest.raises(DatabaseSyncScheduleError):
        schedule_database_sync_generation(store, "Owner/Home", Path("home-assistant_v2.db"))


def test_runtime_schedule_uses_existing_target_identity(tmp_path: Path) -> None:
    scheduled_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    with StateStore(tmp_path) as store:
        item = schedule_runtime_sync_generation(store, "Owner/Home", now=scheduled_at)

    assert item.work_kind == "runtime"
    assert item.work_key == runtime_sync_work_key("Owner/Home")
    assert item.status == "pending"
    assert item.created_at == scheduled_at


def test_runtime_schedule_rearms_after_success(tmp_path: Path) -> None:
    first_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=5)
    with StateStore(tmp_path) as store:
        first = schedule_runtime_sync_generation(store, "Owner/Home", now=first_at)
        running = store.claim_work_kind("runtime", now=first_at)
        assert running is not None
        store.complete_work(running, now=first_at)
        second = schedule_runtime_sync_generation(store, "Owner/Home", now=second_at)

    assert second.work_key == first.work_key
    assert second.status == "pending"
    assert second.attempts == 0
    assert second.created_at == second_at


def test_runtime_schedule_preserves_blocked_failure(tmp_path: Path) -> None:
    first_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=5)
    with StateStore(tmp_path) as store:
        schedule_runtime_sync_generation(store, "Owner/Home", now=first_at)
        running = store.claim_work_kind("runtime", now=first_at)
        assert running is not None
        blocked = store.fail_work(running, transient=False, now=first_at)
        repeated = schedule_runtime_sync_generation(store, "Owner/Home", now=second_at)

    assert repeated == blocked
    assert repeated.status == "blocked"


def test_runtime_schedule_rejects_invalid_target_without_disclosure(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store, pytest.raises(RuntimeSyncScheduleError) as error:
        schedule_runtime_sync_generation(store, " secret-sentinel ")
    assert "secret-sentinel" not in str(error.value)
