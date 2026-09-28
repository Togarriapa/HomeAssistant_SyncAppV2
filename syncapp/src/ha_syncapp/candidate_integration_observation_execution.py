"""Bounded execution of post-restart integration initialization observation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .core_health_window import CoreHealthWindowError, load_core_health_window
from .core_restart_transport import CoreRestartError, load_core_restart_attempt
from .integration_observation import (
    IntegrationObservationError,
    SessionFactory,
    load_integration_observation,
    observe_integrations_once,
)
from .post_apply_activation import (
    PostApplyActivationError,
    load_post_apply_activation_authorization,
)
from .state import StateError, StateStore, WorkItem
from .supervisor_health_observation import (
    SupervisorHealthError,
    load_supervisor_health_observation,
)


class CandidateIntegrationObservationExecutionError(RuntimeError):
    """Integration observation work could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateIntegrationObservationExecutionResult:
    """Content-minimal outcome of one bounded integration observation."""

    deployment_id: str
    action: str
    replayed: bool
    entry_count: int
    disabled_count: int
    work: WorkItem
    successor: WorkItem


def execute_candidate_integration_observation_once(
    store: StateStore,
    item: WorkItem,
    *,
    token: str | None = None,
    session_factory: SessionFactory | None = None,
    now: datetime | None = None,
) -> CandidateIntegrationObservationExecutionResult:
    """Read integration state at most once and schedule startup-log observation."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_observe_integrations"
        or item.status != "running"
        or item.attempts < 1
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _reject(False)
    when = when.astimezone(UTC)
    try:
        _prove_chain(store, item.work_key)
        observed = observe_integrations_once(
            store,
            item.work_key,
            token=token,
            session_factory=session_factory,
            observed_at=when,
        )
        work, successor = _finish(store, item, when)
        return CandidateIntegrationObservationExecutionResult(
            item.work_key,
            "startup_error_observation_scheduled",
            observed.replayed,
            observed.entry_count,
            observed.disabled_count,
            work,
            successor,
        )
    except CandidateIntegrationObservationExecutionError:
        raise
    except IntegrationObservationError as error:
        _reject(error.transient)
    except (
        CoreHealthWindowError,
        CoreRestartError,
        PostApplyActivationError,
        SupervisorHealthError,
        StateError,
        sqlite3.Error,
    ):
        _reject(False)


def _prove_chain(store: StateStore, deployment_id: str) -> None:
    authorization = load_post_apply_activation_authorization(store, deployment_id)
    restart = load_core_restart_attempt(store, deployment_id)
    window = load_core_health_window(store, deployment_id)
    supervisor = load_supervisor_health_observation(store, deployment_id)
    if (
        authorization is None
        or restart is None
        or restart.phase != "request_acknowledged"
        or restart.authorization_record_sha256 != authorization.record_sha256
        or window is None
        or window.completed_at is None
        or supervisor is None
        or supervisor.core_window_sha256 != window.record_sha256
    ):
        _reject(False)


def _finish(
    store: StateStore,
    item: WorkItem,
    when: datetime,
) -> tuple[WorkItem, WorkItem]:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _prove_chain(store, item.work_key)
            integration = load_integration_observation(store, item.work_key)
            if integration is None:
                _reject(False)
            existing = db.execute(
                "SELECT 1 FROM work WHERE work_kind='candidate_observe_startup_errors' "
                "AND work_key=?",
                (item.work_key,),
            ).fetchone()
            if existing is not None:
                _reject(False)
            current = when.isoformat()
            db.execute(
                "INSERT INTO work (work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at) VALUES ('candidate_observe_startup_errors',?,'pending',0,?,?,?)",
                (item.work_key, current, current, current),
            )
            changed = db.execute(
                "UPDATE work SET status='succeeded',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate_observe_integrations' AND work_key=? "
                "AND status='running' AND attempts=?",
                (current, item.work_key, item.attempts),
            )
            if changed.rowcount != 1:
                _reject(False)
        work = store._get_work(item.work_kind, item.work_key)
        successor = store._get_work("candidate_observe_startup_errors", item.work_key)
        if work.status != "succeeded" or successor.status != "pending":
            _reject(False)
        return work, successor
    except CandidateIntegrationObservationExecutionError:
        raise
    except (
        CoreHealthWindowError,
        CoreRestartError,
        IntegrationObservationError,
        PostApplyActivationError,
        SupervisorHealthError,
        StateError,
        sqlite3.Error,
    ):
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidateIntegrationObservationExecutionError(
        "Candidate integration observation failed closed",
        transient=transient,
    ) from None
