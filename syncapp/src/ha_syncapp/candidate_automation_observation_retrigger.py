"""Bounded Retrigger lane for automation/script load observation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_automation_observation_execution import (
    CandidateAutomationObservationExecutionError,
    CandidateAutomationObservationExecutionResult,
    execute_candidate_automation_observation_once,
)
from .state import StateError, StateStore, WorkItem


class CandidateAutomationObservationRetriggerError(RuntimeError):
    """Automation/script observation recovery failed closed."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateAutomationObservationRetriggerResult:
    """Bounded, identity-free outcome of one automation-observation pass."""

    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateAutomationObservationExecutionResult]


def run_candidate_automation_observation_retrigger_pass(
    store: StateStore,
    *,
    token: str | None = None,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_automation_observation_once,
) -> CandidateAutomationObservationRetriggerResult:
    """Recover interrupted automation work and process at most one exact item."""
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
                "WHERE work_kind='candidate_observe_automation_scripts' "
                "AND status='running'",
                (current, current),
            ).rowcount
            rows = db.execute(
                "SELECT work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at FROM work "
                "WHERE work_kind='candidate_observe_automation_scripts' "
                "AND status IN ('pending','retry') AND next_attempt_at<=? "
                "ORDER BY next_attempt_at,created_at,work_key LIMIT 2",
                (current,),
            ).fetchall()
            if not rows:
                return CandidateAutomationObservationRetriggerResult(recovered, 0, None)
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
        result = executor(store, claimed, token=token, now=when)
        current_work = store._get_work(claimed.work_kind, claimed.work_key)
        expected = {
            "assertion_observation_scheduled": (
                "loaded",
                "candidate_observe_assertions",
            ),
            "finalization_scheduled": ("load_failed", "candidate_finalize"),
        }.get(result.action)
        counts = (result.expected_count, result.loaded_count, result.failed_count)
        if (
            expected is None
            or result.deployment_id != claimed.work_key
            or result.outcome != expected[0]
            or result.work != current_work
            or current_work.status != "succeeded"
            or result.successor.work_kind != expected[1]
            or result.successor.work_key != claimed.work_key
            or result.successor.status != "pending"
            or any(type(value) is not int or value < 0 for value in counts)
            or result.loaded_count + result.failed_count != result.expected_count
            or (
                result.outcome == "loaded"
                and (result.failed_count != 0 or result.loaded_count != result.expected_count)
            )
            or (result.outcome == "load_failed" and result.failed_count == 0)
        ):
            _error(False)
        return CandidateAutomationObservationRetriggerResult(
            recovered,
            len(rows),
            result.action,
        )
    except CandidateAutomationObservationExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        _error(error.transient)
    except CandidateAutomationObservationRetriggerError:
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
    raise CandidateAutomationObservationRetriggerError(
        "candidate automation observation failed closed",
        transient=transient,
    ) from None
