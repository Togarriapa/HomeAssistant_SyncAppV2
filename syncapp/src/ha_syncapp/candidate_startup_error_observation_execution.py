"""Bounded execution of post-restart startup-error observation."""

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
)
from .post_apply_activation import (
    PostApplyActivationError,
    load_post_apply_activation_authorization,
)
from .startup_error_observation import (
    StartupErrorObservationError,
    load_startup_error_observation,
    observe_startup_errors_once,
)
from .state import StateError, StateStore, WorkItem
from .supervisor_health_observation import (
    SupervisorHealthError,
    load_supervisor_health_observation,
)


class CandidateStartupErrorObservationExecutionError(RuntimeError):
    """Startup-error observation work could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateStartupErrorObservationExecutionResult:
    """Content-minimal outcome of one bounded startup-error observation."""

    deployment_id: str
    action: str
    outcome: str
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
    """Read startup logs at most once and schedule the exact outcome successor."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_observe_startup_errors"
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
        observed = observe_startup_errors_once(
            store,
            item.work_key,
            token=token,
            session_factory=session_factory,
            observed_at=when,
        )
        action = (
            "resource_observation_scheduled"
            if observed.status == "clear"
            else "finalization_scheduled"
        )
        work, successor = _finish(store, item, when, observed.status)
        return CandidateStartupErrorObservationExecutionResult(
            item.work_key,
            action,
            observed.status,
            observed.replayed,
            observed.inspected_count,
            observed.warning_count,
            observed.significant_error_count,
            work,
            successor,
        )
    except CandidateStartupErrorObservationExecutionError:
        raise
    except StartupErrorObservationError as error:
        _reject(error.transient)
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


def _prove_chain(store: StateStore, deployment_id: str) -> None:
    authorization = load_post_apply_activation_authorization(store, deployment_id)
    restart = load_core_restart_attempt(store, deployment_id)
    window = load_core_health_window(store, deployment_id)
    supervisor = load_supervisor_health_observation(store, deployment_id)
    integration = load_integration_observation(store, deployment_id)
    if (
        authorization is None
        or restart is None
        or restart.phase != "request_acknowledged"
        or restart.authorization_record_sha256 != authorization.record_sha256
        or window is None
        or window.completed_at is None
        or supervisor is None
        or supervisor.core_window_sha256 != window.record_sha256
        or integration is None
        or integration.supervisor_health_sha256 != supervisor.record_sha256
    ):
        _reject(False)


def _finish(
    store: StateStore,
    item: WorkItem,
    when: datetime,
    outcome: str,
) -> tuple[WorkItem, WorkItem]:
    successor_kind = {
        "clear": "candidate_observe_resources",
        "significant_errors": "candidate_finalize",
    }.get(outcome)
    if successor_kind is None:
        _reject(False)
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _prove_chain(store, item.work_key)
            observation = load_startup_error_observation(store, item.work_key)
            if (
                observation is None
                or (outcome == "clear" and observation.significant_error_count != 0)
                or (outcome == "significant_errors" and observation.significant_error_count == 0)
            ):
                _reject(False)
            conflicts = db.execute(
                "SELECT COUNT(*) FROM work WHERE work_key=? AND work_kind IN "
                "('candidate_observe_resources','candidate_finalize')",
                (item.work_key,),
            ).fetchone()
            if conflicts is None or conflicts[0] != 0:
                _reject(False)
            current = when.isoformat()
            db.execute(
                "INSERT INTO work (work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at) VALUES (?,?,'pending',0,?,?,?)",
                (successor_kind, item.work_key, current, current, current),
            )
            changed = db.execute(
                "UPDATE work SET status='succeeded',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate_observe_startup_errors' AND work_key=? "
                "AND status='running' AND attempts=?",
                (current, item.work_key, item.attempts),
            )
            if changed.rowcount != 1:
                _reject(False)
        work = store._get_work(item.work_kind, item.work_key)
        successor = store._get_work(successor_kind, item.work_key)
        if work.status != "succeeded" or successor.status != "pending":
            _reject(False)
        return work, successor
    except CandidateStartupErrorObservationExecutionError:
        raise
    except (
        CoreHealthWindowError,
        CoreRestartError,
        IntegrationObservationError,
        PostApplyActivationError,
        StartupErrorObservationError,
        SupervisorHealthError,
        StateError,
        sqlite3.Error,
    ):
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidateStartupErrorObservationExecutionError(
        "Candidate startup-error observation failed closed",
        transient=transient,
    ) from None
