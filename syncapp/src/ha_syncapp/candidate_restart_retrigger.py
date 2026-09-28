"""Bounded Retrigger lane for authorized candidate Core restarts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from .candidate_restart_execution import (
    CandidateRestartExecutionError,
    CandidateRestartExecutionResult,
    execute_candidate_restart_once,
)
from .state import StateError, StateStore, WorkItem


class CandidateRestartRetriggerError(RuntimeError):
    """Candidate restart recovery failed closed."""


@dataclass(frozen=True, slots=True)
class CandidateRestartRetriggerResult:
    """Bounded, identity-free outcome of one restart recovery pass."""

    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateRestartExecutionResult]


def run_candidate_restart_retrigger_pass(
    store: StateStore,
    *,
    token: str | None = None,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_restart_once,
) -> CandidateRestartRetriggerResult:
    """Recover interrupted restart work and process at most one exact item."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if (
        type(store) is not StateStore
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        raise CandidateRestartRetriggerError("candidate restart Retrigger inputs are invalid")
    try:
        current = when.astimezone(UTC).isoformat()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            recovered = db.execute(
                "UPDATE work SET status='retry',updated_at=?,next_attempt_at=? "
                "WHERE work_kind='candidate_restart' AND status='running'",
                (current, current),
            ).rowcount
            rows = db.execute(
                "SELECT work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at FROM work WHERE work_kind='candidate_restart' "
                "AND status IN ('pending','retry') AND next_attempt_at<=? "
                "ORDER BY next_attempt_at,created_at,work_key LIMIT 2",
                (current,),
            ).fetchall()
            if not rows:
                return CandidateRestartRetriggerResult(recovered, 0, None)
            item = store._work_from_row(rows[0])
            changed = db.execute(
                "UPDATE work SET status='running',attempts=attempts+1,updated_at=?,"
                "next_attempt_at=NULL WHERE work_kind=? AND work_key=? "
                "AND status=? AND attempts=?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if changed.rowcount != 1:
                raise CandidateRestartRetriggerError("candidate restart claim changed")
        claimed = store._get_work(item.work_kind, item.work_key)
        result = executor(store, claimed, token=token, now=when)
        current_work = store._get_work(claimed.work_kind, claimed.work_key)
        expected = "succeeded" if result.action == "observation_scheduled" else "blocked"
        if (
            result.deployment_id != claimed.work_key
            or result.action
            not in {"observation_scheduled", "blocked_uncertain", "blocked_invalid"}
            or result.work != current_work
            or current_work.status != expected
            or (
                result.action == "observation_scheduled"
                and (result.successor is None or result.successor.status != "pending")
            )
            or (result.action != "observation_scheduled" and result.successor is not None)
        ):
            raise CandidateRestartRetriggerError("candidate restart completion is invalid")
        return CandidateRestartRetriggerResult(recovered, len(rows), result.action)
    except CandidateRestartExecutionError:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise CandidateRestartRetriggerError("candidate restart failed closed") from None
    except (CandidateRestartRetriggerError, StateError, sqlite3.Error):
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise CandidateRestartRetriggerError("candidate restart failed closed") from None
