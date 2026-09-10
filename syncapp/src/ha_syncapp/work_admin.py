"""Explicit administrative controls for one exact durable work item."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .state import StateError, StateStore, WorkItem, _timestamp, _validate_work_identity


class WorkAdministrationError(RuntimeError):
    """An explicit durable-work administrative action could not be applied safely."""


def retry_blocked_work(
    store: StateStore,
    work_kind: str,
    work_key: str,
    *,
    now: datetime | None = None,
) -> WorkItem:
    """Explicitly make exactly one blocked work item eligible for a fresh retry budget."""
    if type(store) is not StateStore:
        raise WorkAdministrationError("administrative retry state store is invalid")
    try:
        _validate_work_identity(work_kind, work_key)
        retry_time = _timestamp(now)
        current = retry_time.isoformat()
        with store._connection as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT work_kind, work_key, status, attempts, created_at, updated_at, "
                "next_attempt_at FROM work WHERE work_kind = ? AND work_key = ?",
                (work_kind, work_key),
            ).fetchone()
            if row is None:
                raise WorkAdministrationError("administrative retry work item does not exist")
            blocked = store._work_from_row(row)
            if blocked.status != "blocked":
                raise WorkAdministrationError("only blocked work can be administratively retried")

            result = database.execute(
                "UPDATE work SET status = 'pending', attempts = 0, updated_at = ?, "
                "next_attempt_at = ? WHERE work_kind = ? AND work_key = ? "
                "AND status = 'blocked' AND attempts = ?",
                (current, current, work_kind, work_key, blocked.attempts),
            )
            if result.rowcount != 1:
                raise WorkAdministrationError("administrative retry work item changed unexpectedly")

            updated_row = database.execute(
                "SELECT work_kind, work_key, status, attempts, created_at, updated_at, "
                "next_attempt_at FROM work WHERE work_kind = ? AND work_key = ?",
                (work_kind, work_key),
            ).fetchone()
            if updated_row is None:
                raise WorkAdministrationError("administrative retry work item disappeared")
            retried = store._work_from_row(updated_row)
            if (
                retried.status != "pending"
                or retried.attempts != 0
                or retried.created_at != blocked.created_at
                or retried.updated_at != retry_time
                or retried.next_attempt_at != retry_time
            ):
                raise WorkAdministrationError("administrative retry durable state is inconsistent")
        return retried
    except WorkAdministrationError:
        raise
    except (StateError, sqlite3.Error) as exc:
        raise WorkAdministrationError("administrative retry failed closed") from exc
