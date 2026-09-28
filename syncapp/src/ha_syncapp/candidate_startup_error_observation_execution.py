"""Bounded execution of post-restart startup-error observation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .integration_observation import SessionFactory
from .startup_error_observation import (
    StartupErrorObservationError,
    load_startup_error_observation,
    observe_startup_errors_once,
)
from .state import StateError, StateStore, WorkItem


class CandidateStartupErrorObservationExecutionError(RuntimeError):
    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateStartupErrorObservationExecutionResult:
    deployment_id: str
    action: str
    replayed: bool
    inspected_count: int
    warning_count: int
    significant_error_count: int
    work: WorkItem
    successor: WorkItem


def execute_candidate_startup_error_observation_once(
    store: StateStore,
    item: WorkItem,
    *,
    token: str | None = None,
    session_factory: SessionFactory | None = None,
    now: datetime | None = None,
) -> CandidateStartupErrorObservationExecutionResult:
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_observe_startup_errors"
        or item.status != "running"
        or item.attempts < 1
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _reject(False)
    when = when.astimezone(UTC)
    try:
        result = observe_startup_errors_once(
            store, item.work_key, token=token, session_factory=session_factory, observed_at=when
        )
        if result.status != "clear" or result.significant_error_count != 0:
            _reject(False)
        work, successor = _finish(store, item, when)
        return CandidateStartupErrorObservationExecutionResult(
            item.work_key,
            "resource_observation_scheduled",
            result.replayed,
            result.inspected_count,
            result.warning_count,
            result.significant_error_count,
            work,
            successor,
        )
    except CandidateStartupErrorObservationExecutionError:
        raise
    except StartupErrorObservationError as error:
        _reject(error.transient)
    except (StateError, sqlite3.Error):
        _reject(False)


def _finish(store: StateStore, item: WorkItem, when: datetime) -> tuple[WorkItem, WorkItem]:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            startup = load_startup_error_observation(store, item.work_key)
            if startup is None or startup.significant_error_count:
                _reject(False)
            if db.execute(
                "SELECT 1 FROM work WHERE work_kind=? AND work_key=?",
                ("candidate_observe_resources", item.work_key),
            ).fetchone():
                _reject(False)
            current = when.isoformat()
            db.execute(
                "INSERT INTO work (work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at) VALUES (?,?, 'pending',0,?,?,?)",
                ("candidate_observe_resources", item.work_key, current, current, current),
            )
            changed = db.execute(
                "UPDATE work SET status='succeeded',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind=? AND work_key=? AND status='running' AND attempts=?",
                (current, item.work_kind, item.work_key, item.attempts),
            )
            if changed.rowcount != 1:
                _reject(False)
        return (
            store._get_work(item.work_kind, item.work_key),
            store._get_work("candidate_observe_resources", item.work_key),
        )
    except CandidateStartupErrorObservationExecutionError:
        raise
    except (StartupErrorObservationError, StateError, sqlite3.Error):
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidateStartupErrorObservationExecutionError(
        "Candidate startup-error observation failed closed", transient=transient
    ) from None
