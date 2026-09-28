"""Bounded Retrigger lane for the post-restart Core health window."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_core_observation_execution import (
    CandidateCoreObservationExecutionError,
    CandidateCoreObservationExecutionResult,
    execute_candidate_core_observation_once,
)
from .state import StateError, StateStore, WorkItem


class CandidateCoreObservationRetriggerError(RuntimeError):
    """Core observation recovery failed closed."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateCoreObservationRetriggerResult:
    """Bounded, identity-free outcome of one Core observation pass."""

    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateCoreObservationExecutionResult]


def run_candidate_core_observation_retrigger_pass(
    store: StateStore,
    *,
    observation_seconds: int,
    token: str | None = None,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_core_observation_once,
) -> CandidateCoreObservationRetriggerResult:
    """Recover interrupted observation work and process at most one exact item."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if (
        type(store) is not StateStore
        or type(observation_seconds) is not int
        or not 30 <= observation_seconds <= 3600
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        raise CandidateCoreObservationRetriggerError(
            "candidate Core observation Retrigger inputs are invalid",
            transient=False,
        )
    try:
        current = when.astimezone(UTC).isoformat()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            recovered = db.execute(
                "UPDATE work SET status='retry',updated_at=?,next_attempt_at=? "
                "WHERE work_kind='candidate_observe' AND status='running'",
                (current, current),
            ).rowcount
            rows = db.execute(
                "SELECT work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at FROM work WHERE work_kind='candidate_observe' "
                "AND status IN ('pending','retry') AND next_attempt_at<=? "
                "ORDER BY next_attempt_at,created_at,work_key LIMIT 2",
                (current,),
            ).fetchall()
            if not rows:
                return CandidateCoreObservationRetriggerResult(recovered, 0, None)
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
        result = executor(
            store,
            claimed,
            observation_seconds=observation_seconds,
            token=token,
            now=when,
        )
        current_work = store._get_work(claimed.work_kind, claimed.work_key)
        expected = {
            "initial_health_recorded": "pending",
            "window_started": "retry",
            "waiting": "retry",
            "supervisor_observation_scheduled": "succeeded",
        }.get(result.action)
        if (
            result.deployment_id != claimed.work_key
            or expected is None
            or result.work != current_work
            or current_work.status != expected
            or (
                result.action == "supervisor_observation_scheduled"
                and (
                    result.successor is None
                    or result.successor.work_kind != "candidate_observe_supervisor"
                    or result.successor.status != "pending"
                )
            )
            or (
                result.action != "supervisor_observation_scheduled" and result.successor is not None
            )
        ):
            _error(False)
        return CandidateCoreObservationRetriggerResult(recovered, len(rows), result.action)
    except CandidateCoreObservationExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        _error(error.transient)
    except CandidateCoreObservationRetriggerError:
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
    raise CandidateCoreObservationRetriggerError(
        "candidate Core observation failed closed",
        transient=transient,
    ) from None
