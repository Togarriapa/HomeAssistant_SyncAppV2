"""Bounded Retrigger lane for one exact candidate risk action."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from .candidate_orchestration import CandidateOrchestrationError, load_candidate_orchestration
from .candidate_risk_execution import (
    CandidateRiskExecutionError,
    CandidateRiskExecutionResult,
    execute_candidate_risk_once,
)
from .state import StateError, StateStore, WorkItem


class CandidateRiskRetriggerError(RuntimeError):
    """Candidate risk recovery failed closed."""


@dataclass(frozen=True, slots=True)
class CandidateRiskRetriggerResult:
    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateRiskExecutionResult]


def run_candidate_risk_retrigger_pass(
    store: StateStore,
    *,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_risk_once,
) -> CandidateRiskRetriggerResult:
    """Recover candidate work and execute at most one eligible risk action."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if type(store) is not StateStore or when.tzinfo is None or when.utcoffset() is None:
        raise CandidateRiskRetriggerError("candidate risk inputs are invalid")
    try:
        current = when.astimezone(UTC).isoformat()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            recovered = db.execute(
                "UPDATE work SET status='retry',updated_at=?,next_attempt_at=? "
                "WHERE work_kind='candidate' AND status='running' AND work_key IN "
                "(SELECT candidate_sha FROM candidate_orchestration "
                "WHERE next_action='classify_risk')",
                (current, current),
            ).rowcount
            rows = db.execute(
                "SELECT w.work_kind,w.work_key,w.status,w.attempts,w.created_at,w.updated_at,"
                "w.next_attempt_at FROM work w JOIN candidate_orchestration c "
                "ON c.candidate_sha=w.work_key WHERE w.work_kind='candidate' "
                "AND w.status IN ('pending','retry') AND w.next_attempt_at<=? "
                "AND c.next_action='classify_risk' ORDER BY w.next_attempt_at,w.created_at,"
                "w.work_key LIMIT 2",
                (current,),
            ).fetchall()
            if not rows:
                return CandidateRiskRetriggerResult(recovered, 0, None)
            item = store._work_from_row(rows[0])
            changed = db.execute(
                "UPDATE work SET status='running',attempts=attempts+1,updated_at=?,"
                "next_attempt_at=NULL WHERE work_kind=? AND work_key=? AND status=? AND attempts=?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if changed.rowcount != 1:
                raise CandidateRiskRetriggerError("candidate risk claim changed")
        claimed = store._get_work(item.work_kind, item.work_key)
        orchestration = load_candidate_orchestration(store, claimed.work_key)
        if orchestration is None:
            raise CandidateRiskRetriggerError("candidate risk authority is unavailable")
        result = executor(store, orchestration, now=when)
        store.defer_work(claimed, now=when)
        return CandidateRiskRetriggerResult(recovered, len(rows), result.checkpoint.phase)
    except CandidateRiskExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        raise CandidateRiskRetriggerError("candidate risk failed closed") from None
    except (CandidateRiskRetriggerError, CandidateOrchestrationError, StateError, sqlite3.Error):
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise CandidateRiskRetriggerError("candidate risk failed closed") from None
