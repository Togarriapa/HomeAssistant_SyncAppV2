"""Routine work scheduling kept separate from Retrigger recovery and administration."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .state import StateError, StateStore, WorkItem, _timestamp, _validate_work_identity


class RoutineWorkScheduleError(RuntimeError):
    """A routine work generation could not be scheduled safely."""


def schedule_routine_work(
    store: StateStore,
    work_kind: str,
    work_key: str,
    *,
    now: datetime | None = None,
) -> WorkItem:
    """Schedule one normal work generation without altering recovery semantics.

    Missing work is created as pending. A successfully completed identity starts a
    fresh pending generation. Existing pending/running/retry work is coalesced,
    and blocked work remains blocked until an explicit administrative retry.
    """
    if type(store) is not StateStore:
        raise RoutineWorkScheduleError("routine work state store is invalid")

    try:
        _validate_work_identity(work_kind, work_key)
        scheduled_at = _timestamp(now)
        current = scheduled_at.isoformat()
        with store._connection as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT work_kind, work_key, status, attempts, created_at, updated_at, "
                "next_attempt_at FROM work WHERE work_kind = ? AND work_key = ?",
                (work_kind, work_key),
            ).fetchone()

            if row is None:
                database.execute(
                    "INSERT INTO work (work_kind, work_key, status, attempts, created_at, "
                    "updated_at, next_attempt_at) VALUES (?, ?, 'pending', 0, ?, ?, ?)",
                    (work_kind, work_key, current, current, current),
                )
            else:
                existing = store._work_from_row(row)
                if existing.status == "succeeded":
                    result = database.execute(
                        "UPDATE work SET status = 'pending', attempts = 0, created_at = ?, "
                        "updated_at = ?, next_attempt_at = ? WHERE work_kind = ? "
                        "AND work_key = ? AND status = 'succeeded' AND attempts = ?",
                        (
                            current,
                            current,
                            current,
                            work_kind,
                            work_key,
                            existing.attempts,
                        ),
                    )
                    if result.rowcount != 1:
                        raise RoutineWorkScheduleError(
                            "routine work generation changed unexpectedly"
                        )
                elif existing.status in {"pending", "running", "retry", "blocked"}:
                    return existing
                else:
                    raise RoutineWorkScheduleError("routine work durable state is invalid")

            updated_row = database.execute(
                "SELECT work_kind, work_key, status, attempts, created_at, updated_at, "
                "next_attempt_at FROM work WHERE work_kind = ? AND work_key = ?",
                (work_kind, work_key),
            ).fetchone()
            if updated_row is None:
                raise RoutineWorkScheduleError("routine work generation disappeared")
            scheduled = store._work_from_row(updated_row)
            if (
                scheduled.status != "pending"
                or scheduled.attempts != 0
                or scheduled.created_at != scheduled_at
                or scheduled.updated_at != scheduled_at
                or scheduled.next_attempt_at != scheduled_at
            ):
                raise RoutineWorkScheduleError("routine work durable state is inconsistent")
        return scheduled
    except RoutineWorkScheduleError:
        raise
    except (StateError, sqlite3.Error) as exc:
        raise RoutineWorkScheduleError("routine work scheduling failed closed") from exc
