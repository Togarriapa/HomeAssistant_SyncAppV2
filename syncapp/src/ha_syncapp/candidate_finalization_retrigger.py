"""Bounded Retrigger lane for immutable deployment finalization."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_finalization_execution import (
    CandidateFinalizationExecutionError,
    CandidateFinalizationExecutionResult,
    execute_candidate_finalization_once,
)
from .state import StateError, StateStore, WorkItem


class CandidateFinalizationRetriggerError(RuntimeError):
    """Deployment finalization recovery failed closed."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateFinalizationRetriggerResult:
    """Bounded, identity-free result of one finalization recovery pass."""

    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateFinalizationExecutionResult]


def run_candidate_finalization_retrigger_pass(
    store: StateStore,
    *,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_finalization_once,
) -> CandidateFinalizationRetriggerResult:
    """Recover interrupted finalization work and process at most one item."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if (
        type(store) is not StateStore
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _error(False)
    try:
        current = when.astimezone(UTC).isoformat()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            recovered = db.execute(
                "UPDATE work SET status='retry',updated_at=?,next_attempt_at=? "
                "WHERE work_kind='candidate_finalize' AND status='running'",
                (current, current),
            ).rowcount
            rows = db.execute(
                "SELECT work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at FROM work WHERE work_kind='candidate_finalize' "
                "AND status IN ('pending','retry') AND next_attempt_at<=? "
                "ORDER BY next_attempt_at,created_at,work_key LIMIT 2",
                (current,),
            ).fetchall()
            if not rows:
                return CandidateFinalizationRetriggerResult(recovered, 0, None)
            item = store._work_from_row(rows[0])
            changed = db.execute(
                "UPDATE work SET status='running',attempts=attempts+1,updated_at=?,"
                "next_attempt_at=NULL WHERE work_kind=? AND work_key=? "
                "AND status=? AND attempts=?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if changed.rowcount != 1:
                _error(False)
        claimed = store._get_work(item.work_kind, item.work_key)
        result = executor(store, claimed, now=when)
        current_work = store._get_work(claimed.work_kind, claimed.work_key)
        success = (
            result.action == "promotion_scheduled"
            and result.outcome == "success"
            and result.authority == "promote_and_tag"
            and result.failure_stage == "none"
            and result.candidate_blocked is False
            and result.successor.work_kind == "candidate_promote"
            and result.completed_predicate_count == 5
        )
        failure = (
            result.action == "rollback_scheduled"
            and result.outcome == "failure"
            and result.authority == "rollback"
            and result.failure_stage != "none"
            and result.candidate_blocked is True
            and result.successor.work_kind == "candidate_rollback"
            and 1 <= result.completed_predicate_count <= 5
        )
        if (
            not (success or failure)
            or result.deployment_id != claimed.work_key
            or result.work != current_work
            or current_work.status != "succeeded"
            or result.successor.work_key != claimed.work_key
            or result.successor.status != "pending"
        ):
            _error(False)
        return CandidateFinalizationRetriggerResult(recovered, len(rows), result.action)
    except CandidateFinalizationExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        _error(error.transient)
    except CandidateFinalizationRetriggerError:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise
    except (StateError, sqlite3.Error):
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        _error(False)


def _error(transient: bool) -> NoReturn:
    raise CandidateFinalizationRetriggerError(
        "candidate deployment finalization failed closed", transient=transient
    ) from None
