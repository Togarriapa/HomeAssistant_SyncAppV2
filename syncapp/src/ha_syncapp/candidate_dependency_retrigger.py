"""Bounded Retrigger lane for one exact candidate dependency action."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .candidate_dependency_execution import (
    CandidateDependencyExecutionError,
    CandidateDependencyExecutionResult,
    execute_candidate_dependency_once,
)
from .candidate_orchestration import CandidateOrchestrationError, load_candidate_orchestration
from .state import StateError, StateStore, WorkItem

_WORK_KIND = "candidate"


class CandidateDependencyRetriggerError(RuntimeError):
    """Candidate dependency recovery could not safely complete."""


@dataclass(frozen=True, slots=True)
class CandidateDependencyRetriggerResult:
    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateDependencyExecutionResult]


def run_candidate_dependency_retrigger_pass(
    store: StateStore,
    home_assistant_root: Path,
    staging_root: Path,
    core_token: str | None,
    *,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_dependency_once,
) -> CandidateDependencyRetriggerResult:
    """Recover candidate work and execute at most one eligible dependency action."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if type(store) is not StateStore or when.tzinfo is None or when.utcoffset() is None:
        raise CandidateDependencyRetriggerError("candidate dependency inputs are invalid")
    try:
        recovered = _recover_interrupted_candidate_work(store, when)
        considered, claimed = _claim_eligible_candidate(store, when)
        if claimed is None:
            return CandidateDependencyRetriggerResult(recovered, considered, None)
        orchestration = load_candidate_orchestration(store, claimed.work_key)
        if orchestration is None:
            raise CandidateDependencyRetriggerError("candidate dependency authority is unavailable")
        result = executor(
            store,
            orchestration,
            core_token=core_token,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            now=when,
        )
        store.defer_work(claimed, now=when)
        return CandidateDependencyRetriggerResult(recovered, considered, result.checkpoint.phase)
    except CandidateDependencyExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        raise CandidateDependencyRetriggerError("candidate dependency failed closed") from None
    except (CandidateDependencyRetriggerError, CandidateOrchestrationError):
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise CandidateDependencyRetriggerError("candidate dependency failed closed") from None
    except (StateError, sqlite3.Error, OSError):
        raise CandidateDependencyRetriggerError("candidate dependency failed closed") from None


def _recover_interrupted_candidate_work(store: StateStore, when: datetime) -> int:
    try:
        current = when.astimezone(UTC).isoformat()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE work SET status = 'retry', updated_at = ?, next_attempt_at = ? "
                "WHERE work_kind = ? AND status = 'running' AND work_key IN "
                "(SELECT candidate_sha FROM candidate_orchestration "
                "WHERE next_action = 'analyze_dependencies')",
                (current, current, _WORK_KIND),
            )
        return result.rowcount
    except sqlite3.Error:
        raise CandidateDependencyRetriggerError(
            "candidate dependency recovery is unavailable"
        ) from None


def _claim_eligible_candidate(store: StateStore, when: datetime) -> tuple[int, WorkItem | None]:
    current = when.astimezone(UTC).isoformat()
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT w.work_kind, w.work_key, w.status, w.attempts, w.created_at, "
                "w.updated_at, w.next_attempt_at FROM work AS w "
                "JOIN candidate_orchestration AS c ON c.candidate_sha = w.work_key "
                "WHERE w.work_kind = ? AND w.status IN ('pending','retry') "
                "AND w.next_attempt_at <= ? AND c.next_action = 'analyze_dependencies' "
                "ORDER BY w.next_attempt_at, w.created_at, w.work_key LIMIT 2",
                (_WORK_KIND, current),
            ).fetchall()
            considered = len(rows)
            if not rows:
                return 0, None
            item = store._work_from_row(rows[0])
            result = db.execute(
                "UPDATE work SET status = 'running', attempts = attempts + 1, "
                "updated_at = ?, next_attempt_at = NULL WHERE work_kind = ? "
                "AND work_key = ? AND status = ? AND attempts = ?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if result.rowcount != 1:
                raise CandidateDependencyRetriggerError(
                    "candidate dependency claim changed unexpectedly"
                )
        return considered, store._get_work(item.work_kind, item.work_key)
    except sqlite3.Error:
        raise CandidateDependencyRetriggerError(
            "candidate dependency claim is unavailable"
        ) from None
