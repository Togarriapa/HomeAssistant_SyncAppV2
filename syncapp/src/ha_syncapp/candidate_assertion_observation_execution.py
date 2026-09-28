"""Bounded execution of canonical post-deployment assertions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_automation_observation_execution import (
    CandidateAutomationObservationExecutionError,
)
from .candidate_automation_observation_execution import (
    _load_target as _load_automation_target,
)
from .integration_observation import SessionFactory
from .post_deployment_assertion_observation import (
    PostDeploymentAssertionObservationError,
    PostDeploymentAssertionPlan,
    derive_post_deployment_assertion_plan,
    evaluate_post_deployment_assertions_once,
    load_post_deployment_assertion_observation,
)
from .state import StateError, StateStore, WorkItem


class CandidateAssertionObservationExecutionError(RuntimeError):
    """Assertion observation work could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateAssertionObservationExecutionResult:
    """Content-minimal outcome of one post-deployment assertion pass."""

    deployment_id: str
    action: str
    outcome: str
    replayed: bool
    declared_count: int
    derived_count: int
    evaluated_count: int
    passed_count: int
    failed_count: int
    skipped_count: int
    work: WorkItem
    successor: WorkItem


def execute_candidate_assertion_observation_once(
    store: StateStore,
    item: WorkItem,
    *,
    token: str | None = None,
    session_factory: SessionFactory | None = None,
    now: datetime | None = None,
) -> CandidateAssertionObservationExecutionResult:
    """Evaluate canonical assertions and schedule exact finalization work."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_observe_assertions"
        or item.status != "running"
        or item.attempts < 1
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _reject(False)
    when = when.astimezone(UTC)
    try:
        plan = _load_plan(store, item.work_key)
        observed = evaluate_post_deployment_assertions_once(
            store,
            plan,
            token=token,
            session_factory=session_factory,
            observed_at=when,
        )
        work, successor = _finish(store, item, when, plan, observed.status)
        return CandidateAssertionObservationExecutionResult(
            item.work_key,
            "finalization_scheduled",
            observed.status,
            observed.replayed,
            observed.declared_count,
            observed.derived_count,
            observed.evaluated_count,
            observed.passed_count,
            observed.failed_count,
            observed.skipped_count,
            work,
            successor,
        )
    except CandidateAssertionObservationExecutionError:
        raise
    except PostDeploymentAssertionObservationError as error:
        _reject(error.transient)
    except (CandidateAutomationObservationExecutionError, StateError, sqlite3.Error):
        _reject(False)


def _load_plan(store: StateStore, deployment_id: str) -> PostDeploymentAssertionPlan:
    try:
        return derive_post_deployment_assertion_plan(_load_automation_target(store, deployment_id))
    except CandidateAutomationObservationExecutionError:
        _reject(False)


def _finish(
    store: StateStore,
    item: WorkItem,
    when: datetime,
    plan: PostDeploymentAssertionPlan,
    outcome: str,
) -> tuple[WorkItem, WorkItem]:
    if outcome not in {"passed", "failed"}:
        _reject(False)
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            current_plan = _load_plan(store, item.work_key)
            observation = load_post_deployment_assertion_observation(store, current_plan)
            if (
                current_plan != plan
                or observation is None
                or observation.evaluated_count
                != observation.passed_count + observation.failed_count
                or observation.evaluated_count + observation.skipped_count
                != observation.declared_count + observation.derived_count
                or (outcome == "passed" and observation.failed_count != 0)
                or (outcome == "failed" and observation.failed_count == 0)
            ):
                _reject(False)
            conflicts = db.execute(
                "SELECT COUNT(*) FROM work WHERE work_key=? AND work_kind='candidate_finalize'",
                (item.work_key,),
            ).fetchone()
            if conflicts is None or conflicts[0] != 0:
                _reject(False)
            current = when.isoformat()
            db.execute(
                "INSERT INTO work (work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at) VALUES ('candidate_finalize',?,'pending',0,?,?,?)",
                (item.work_key, current, current, current),
            )
            changed = db.execute(
                "UPDATE work SET status='succeeded',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate_observe_assertions' AND work_key=? "
                "AND status='running' AND attempts=?",
                (current, item.work_key, item.attempts),
            )
            if changed.rowcount != 1:
                _reject(False)
        work = store._get_work(item.work_kind, item.work_key)
        successor = store._get_work("candidate_finalize", item.work_key)
        if work.status != "succeeded" or successor.status != "pending":
            _reject(False)
        return work, successor
    except CandidateAssertionObservationExecutionError:
        raise
    except PostDeploymentAssertionObservationError as error:
        _reject(error.transient)
    except (CandidateAutomationObservationExecutionError, StateError, sqlite3.Error):
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidateAssertionObservationExecutionError(
        "Candidate assertion observation failed closed",
        transient=transient,
    ) from None
