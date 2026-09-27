"""Bounded Retrigger lane for one exact candidate static-validation action."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .candidate_orchestration import CandidateOrchestrationError, load_candidate_orchestration
from .candidate_static_execution import (
    CandidateStaticExecutionError,
    CandidateStaticExecutionResult,
    execute_candidate_static_once,
)
from .state import StateError, StateStore, WorkItem


class CandidateStaticRetriggerError(RuntimeError):
    """Candidate static-validation recovery failed closed."""


@dataclass(frozen=True, slots=True)
class CandidateStaticRetriggerResult:
    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateStaticExecutionResult]


def run_candidate_static_retrigger_pass(
    store: StateStore,
    *,
    staging_root: Path,
    home_assistant_root: Path,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_static_once,
) -> CandidateStaticRetriggerResult:
    """Recover candidate work and execute at most one eligible static validation."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if (
        type(store) is not StateStore
        or when.tzinfo is None
        or when.utcoffset() is None
        or not staging_root.is_absolute()
        or not home_assistant_root.is_absolute()
    ):
        raise CandidateStaticRetriggerError("candidate static inputs are invalid")
    try:
        current = when.astimezone(UTC).isoformat()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            recovered = db.execute(
                "UPDATE work SET status='retry',updated_at=?,next_attempt_at=? "
                "WHERE work_kind='candidate' AND status='running' AND work_key IN "
                "(SELECT candidate_sha FROM candidate_orchestration WHERE next_action='validate')",
                (current, current),
            ).rowcount
            rows = db.execute(
                "SELECT w.work_kind,w.work_key,w.status,w.attempts,w.created_at,w.updated_at,"
                "w.next_attempt_at FROM work w JOIN candidate_orchestration c "
                "ON c.candidate_sha=w.work_key WHERE w.work_kind='candidate' "
                "AND w.status IN ('pending','retry') AND w.next_attempt_at<=? "
                "AND c.next_action='validate' ORDER BY w.next_attempt_at,w.created_at,w.work_key "
                "LIMIT 2",
                (current,),
            ).fetchall()
            if not rows:
                return CandidateStaticRetriggerResult(recovered, 0, None)
            item = store._work_from_row(rows[0])
            changed = db.execute(
                "UPDATE work SET status='running',attempts=attempts+1,updated_at=?,"
                "next_attempt_at=NULL WHERE work_kind=? AND work_key=? AND status=? AND attempts=?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if changed.rowcount != 1:
                raise CandidateStaticRetriggerError("candidate static claim changed")
        claimed = store._get_work(item.work_kind, item.work_key)
        orchestration = load_candidate_orchestration(store, claimed.work_key)
        if orchestration is None:
            raise CandidateStaticRetriggerError("candidate static authority is unavailable")
        result = executor(
            store,
            orchestration,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            now=when,
        )
        if result.validation.syntax_valid:
            store.defer_work(claimed, now=when)
        return CandidateStaticRetriggerResult(recovered, len(rows), result.checkpoint.phase)
    except CandidateStaticExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        raise CandidateStaticRetriggerError("candidate static failed closed") from None
    except (CandidateStaticRetriggerError, CandidateOrchestrationError, StateError, sqlite3.Error):
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise CandidateStaticRetriggerError("candidate static failed closed") from None
