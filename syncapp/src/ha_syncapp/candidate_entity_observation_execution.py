"""Bounded execution of affected-entity state observation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_dependency_checkpoint import CandidateDependencyCheckpointError
from .candidate_dependency_execution import (
    CandidateDependencyExecutionError,
    load_candidate_dependency_checkpoint,
)
from .candidate_risk_checkpoint import CandidateRiskCheckpointError
from .candidate_risk_execution import (
    CandidateRiskExecutionError,
    load_candidate_risk_checkpoint,
)
from .entity_state_observation import (
    EntityStateObservationError,
    load_entity_state_observation,
    observe_entity_states_once,
)
from .integration_observation import SessionFactory
from .resource_availability_observation import (
    ResourceAvailabilityError,
    ResourceAvailabilityTarget,
    derive_resource_availability_target,
)
from .state import StateError, StateStore, WorkItem


class CandidateEntityObservationExecutionError(RuntimeError):
    """Affected-entity observation work could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateEntityObservationExecutionResult:
    """Content-minimal outcome of one bounded entity-state observation."""

    deployment_id: str
    action: str
    outcome: str
    replayed: bool
    expected_count: int
    valid_count: int
    invalid_count: int
    work: WorkItem
    successor: WorkItem


def execute_candidate_entity_observation_once(
    store: StateStore,
    item: WorkItem,
    *,
    token: str | None = None,
    session_factory: SessionFactory | None = None,
    now: datetime | None = None,
) -> CandidateEntityObservationExecutionResult:
    """Observe exact affected entity states and schedule the exact successor."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_observe_entities"
        or item.status != "running"
        or item.attempts < 1
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _reject(False)
    when = when.astimezone(UTC)
    try:
        target = _load_target(store, item.work_key)
        observed = observe_entity_states_once(
            store,
            target,
            token=token,
            session_factory=session_factory,
            observed_at=when,
        )
        action = (
            "automation_script_observation_scheduled"
            if observed.status == "valid"
            else "finalization_scheduled"
        )
        work, successor = _finish(store, item, when, target, observed.status)
        return CandidateEntityObservationExecutionResult(
            item.work_key,
            action,
            observed.status,
            observed.replayed,
            observed.expected_count,
            observed.valid_count,
            observed.invalid_count,
            work,
            successor,
        )
    except CandidateEntityObservationExecutionError:
        raise
    except EntityStateObservationError as error:
        _reject(error.transient)
    except (
        CandidateDependencyCheckpointError,
        CandidateDependencyExecutionError,
        CandidateRiskCheckpointError,
        CandidateRiskExecutionError,
        ResourceAvailabilityError,
        StateError,
        sqlite3.Error,
    ):
        _reject(False)


def _load_target(store: StateStore, deployment_id: str) -> ResourceAvailabilityTarget:
    prepared = store.prepared_deployment(deployment_id)
    if prepared is None:
        _reject(False)
    dependency = load_candidate_dependency_checkpoint(store, prepared.evidence.candidate_sha)
    risk_checkpoint = load_candidate_risk_checkpoint(store, prepared.evidence.candidate_sha)
    if (
        dependency is None
        or dependency.phase != "completed"
        or risk_checkpoint is None
        or risk_checkpoint.phase != "completed"
    ):
        _reject(False)
    dependencies = dependency.dependencies()
    runtime = dependency.runtime()
    impact = risk_checkpoint.impact(dependencies, runtime)
    risk = risk_checkpoint.risk(dependencies, runtime)
    return derive_resource_availability_target(
        prepared,
        dependencies,
        impact,
        risk,
        runtime,
    )


def _finish(
    store: StateStore,
    item: WorkItem,
    when: datetime,
    target: ResourceAvailabilityTarget,
    outcome: str,
) -> tuple[WorkItem, WorkItem]:
    successor_kind = {
        "valid": "candidate_observe_automation_scripts",
        "invalid_states": "candidate_finalize",
    }.get(outcome)
    if successor_kind is None:
        _reject(False)
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            current_target = _load_target(store, item.work_key)
            observation = load_entity_state_observation(store, current_target)
            if (
                current_target != target
                or observation is None
                or (
                    outcome == "valid"
                    and (
                        observation.invalid_count != 0
                        or observation.valid_count != observation.expected_count
                    )
                )
                or (outcome == "invalid_states" and observation.invalid_count == 0)
            ):
                _reject(False)
            conflicts = db.execute(
                "SELECT COUNT(*) FROM work WHERE work_key=? AND work_kind IN "
                "('candidate_observe_automation_scripts','candidate_finalize')",
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
                "WHERE work_kind='candidate_observe_entities' AND work_key=? "
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
    except CandidateEntityObservationExecutionError:
        raise
    except (
        CandidateDependencyCheckpointError,
        CandidateDependencyExecutionError,
        CandidateRiskCheckpointError,
        CandidateRiskExecutionError,
        EntityStateObservationError,
        ResourceAvailabilityError,
        StateError,
        sqlite3.Error,
    ):
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidateEntityObservationExecutionError(
        "Candidate entity observation failed closed",
        transient=transient,
    ) from None
