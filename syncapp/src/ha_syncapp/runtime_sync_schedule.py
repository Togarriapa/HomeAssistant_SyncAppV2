"""Routine scheduling adapter for runtime inventory publication work."""

from __future__ import annotations

from datetime import datetime

from .runtime_sync_work import RuntimeSyncWorkError, runtime_sync_work_key
from .state import StateStore, WorkItem
from .work_schedule import RoutineWorkScheduleError, schedule_routine_work


class RuntimeSyncScheduleError(RuntimeError):
    """A routine runtime-sync generation could not be scheduled safely."""


def schedule_runtime_sync_generation(
    store: StateStore,
    target: str,
    *,
    now: datetime | None = None,
) -> WorkItem:
    """Schedule a normal runtime-sync generation without changing Retrigger."""
    try:
        work_key = runtime_sync_work_key(target)
        return schedule_routine_work(store, "runtime", work_key, now=now)
    except (RuntimeSyncWorkError, RoutineWorkScheduleError) as exc:
        raise RuntimeSyncScheduleError("runtime synchronization scheduling failed closed") from exc
