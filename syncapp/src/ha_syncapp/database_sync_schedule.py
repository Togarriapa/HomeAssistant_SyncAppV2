"""Routine scheduling adapter for Recorder database publication work."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .database_sync_work import DatabaseSyncWorkError, database_sync_work_key
from .state import StateStore, WorkItem
from .work_schedule import RoutineWorkScheduleError, schedule_routine_work


class DatabaseSyncScheduleError(RuntimeError):
    """A routine database-sync generation could not be scheduled safely."""


def schedule_database_sync_generation(
    store: StateStore,
    target: str,
    source_database: Path,
    *,
    now: datetime | None = None,
) -> WorkItem:
    """Schedule a normal database-sync generation without changing Retrigger."""
    try:
        work_key = database_sync_work_key(target, source_database)
        return schedule_routine_work(store, "database", work_key, now=now)
    except (DatabaseSyncWorkError, RoutineWorkScheduleError) as exc:
        raise DatabaseSyncScheduleError("database synchronization scheduling failed closed") from exc
