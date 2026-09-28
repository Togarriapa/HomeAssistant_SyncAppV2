"""Bounded execution of the post-restart Core health observation window."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .core_health_observation import (
    CoreHealthError,
    CoreHealthTransport,
    load_core_health_observation,
    observe_core_api_once,
)
from .core_health_window import (
    CoreHealthWindowError,
    advance_core_health_window_once,
    load_core_health_window,
)
from .core_restart_transport import CoreRestartError, load_core_restart_attempt
from .post_apply_activation import (
    PostApplyActivationError,
    load_post_apply_activation_authorization,
)
from .state import StateError, StateStore, WorkItem


class CandidateCoreObservationExecutionError(RuntimeError):
    """The Core observation work could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateCoreObservationExecutionResult:
    """Content-minimal outcome of one bounded Core observation action."""

    deployment_id: str
    action: str
    replayed: bool
    work: WorkItem
    successor: WorkItem | None
    deadline_at: datetime | None


def execute_candidate_core_observation_once(
    store: StateStore,
    item: WorkItem,
    *,
    observation_seconds: int,
    token: str | None = None,
    transport: CoreHealthTransport | None = None,
    now: datetime | None = None,
) -> CandidateCoreObservationExecutionResult:
    """Perform at most one Core request and never sleep inside the worker."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_observe"
        or item.status != "running"
        or item.attempts < 1
        or type(observation_seconds) is not int
        or not 30 <= observation_seconds <= 3600
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _reject(False)
    when = when.astimezone(UTC)
    try:
        _prove_restart_authority(store, item.work_key)
        initial = load_core_health_observation(store, item.work_key)
        if initial is None:
            initial_result = observe_core_api_once(
                store,
                item.work_key,
                token=token,
                transport=transport,
                observed_at=when,
            )
            work = store.defer_work(item, now=when)
            return CandidateCoreObservationExecutionResult(
                item.work_key,
                "initial_health_recorded",
                initial_result.replayed,
                work,
                None,
                None,
            )

        window = load_core_health_window(store, item.work_key)
        if window is None:
            window_result = advance_core_health_window_once(
                store,
                item.work_key,
                observation_seconds=observation_seconds,
                now=when,
            )
            work = _schedule_at(store, item, window_result.deadline_at, when)
            return CandidateCoreObservationExecutionResult(
                item.work_key,
                "window_started",
                window_result.replayed,
                work,
                None,
                window_result.deadline_at,
            )

        if window.completed_at is None and when < window.deadline_at:
            work = _schedule_at(store, item, window.deadline_at, when)
            return CandidateCoreObservationExecutionResult(
                item.work_key,
                "waiting",
                True,
                work,
                None,
                window.deadline_at,
            )

        if window.completed_at is None:
            window_result = advance_core_health_window_once(
                store,
                item.work_key,
                observation_seconds=observation_seconds,
                token=token,
                transport=transport,
                now=when,
            )
            if window_result.status != "healthy":
                _reject(False)
        work, successor = _finish_completed(store, item, when)
        return CandidateCoreObservationExecutionResult(
            item.work_key,
            "supervisor_observation_scheduled",
            window.completed_at is not None,
            work,
            successor,
            window.deadline_at,
        )
    except CandidateCoreObservationExecutionError:
        raise
    except CoreHealthError as error:
        _reject(error.transient)
    except CoreHealthWindowError as error:
        _reject(error.transient)
    except (CoreRestartError, PostApplyActivationError, StateError, sqlite3.Error):
        _reject(False)


def _prove_restart_authority(store: StateStore, deployment_id: str) -> None:
    authorization = load_post_apply_activation_authorization(store, deployment_id)
    restart = load_core_restart_attempt(store, deployment_id)
    if (
        authorization is None
        or restart is None
        or restart.phase != "request_acknowledged"
        or restart.authorization_record_sha256 != authorization.record_sha256
    ):
        _reject(False)


def _schedule_at(
    store: StateStore,
    item: WorkItem,
    deadline: datetime,
    when: datetime,
) -> WorkItem:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _prove_restart_authority(store, item.work_key)
            window = load_core_health_window(store, item.work_key)
            if window is None or window.completed_at is not None or window.deadline_at != deadline:
                _reject(False)
            changed = db.execute(
                "UPDATE work SET status='retry',attempts=0,updated_at=?,next_attempt_at=? "
                "WHERE work_kind='candidate_observe' AND work_key=? AND status='running' "
                "AND attempts=?",
                (when.isoformat(), deadline.isoformat(), item.work_key, item.attempts),
            )
            if changed.rowcount != 1:
                _reject(False)
        work = store._get_work(item.work_kind, item.work_key)
        if work.status != "retry" or work.next_attempt_at != deadline:
            _reject(False)
        return work
    except CandidateCoreObservationExecutionError:
        raise
    except (CoreHealthWindowError, CoreRestartError, PostApplyActivationError, sqlite3.Error):
        _reject(False)


def _finish_completed(
    store: StateStore,
    item: WorkItem,
    when: datetime,
) -> tuple[WorkItem, WorkItem]:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _prove_restart_authority(store, item.work_key)
            window = load_core_health_window(store, item.work_key)
            if window is None or window.completed_at is None:
                _reject(False)
            existing = db.execute(
                "SELECT 1 FROM work WHERE work_kind='candidate_observe_supervisor' AND work_key=?",
                (item.work_key,),
            ).fetchone()
            if existing is not None:
                _reject(False)
            current = when.isoformat()
            db.execute(
                "INSERT INTO work (work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at) VALUES ('candidate_observe_supervisor',?,'pending',0,?,?,?)",
                (item.work_key, current, current, current),
            )
            changed = db.execute(
                "UPDATE work SET status='succeeded',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate_observe' AND work_key=? AND status='running' "
                "AND attempts=?",
                (current, item.work_key, item.attempts),
            )
            if changed.rowcount != 1:
                _reject(False)
        work = store._get_work(item.work_kind, item.work_key)
        successor = store._get_work("candidate_observe_supervisor", item.work_key)
        if work.status != "succeeded" or successor.status != "pending":
            _reject(False)
        return work, successor
    except CandidateCoreObservationExecutionError:
        raise
    except (
        CoreHealthWindowError,
        CoreRestartError,
        PostApplyActivationError,
        sqlite3.Error,
    ):
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidateCoreObservationExecutionError(
        "Candidate Core observation failed closed",
        transient=transient,
    ) from None
