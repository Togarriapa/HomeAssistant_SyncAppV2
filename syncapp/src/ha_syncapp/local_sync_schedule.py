"""Routine scheduling adapter for Local -> Repo B synchronization work."""

from __future__ import annotations

from datetime import datetime

from .local_sync_work import LocalSyncWorkError, local_sync_work_key
from .state import StateStore, WorkItem
from .work_schedule import RoutineWorkScheduleError, schedule_routine_work


class LocalSyncScheduleError(RuntimeError):
    """A routine Local-sync generation could not be scheduled safely."""


def schedule_local_sync_generation(
    store: StateStore,
    target: str,
    *,
    branch: str = "main",
    now: datetime | None = None,
) -> WorkItem:
    """Schedule a normal Local-sync generation without changing Retrigger behavior."""
    try:
        work_key = local_sync_work_key(target, branch)
        return schedule_routine_work(store, "local_sync", work_key, now=now)
    except (LocalSyncWorkError, RoutineWorkScheduleError) as exc:
        raise LocalSyncScheduleError("local synchronization scheduling failed closed") from exc
