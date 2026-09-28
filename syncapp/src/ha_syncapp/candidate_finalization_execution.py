"""Credential-free execution of immutable deployment finalization work."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_assertion_observation_execution import (
    CandidateAssertionObservationExecutionError,
    _load_plan,
)
from .deployment_finalization import (
    DeploymentFinalizationError,
    DeploymentFinalizationResult,
    finalize_deployment_once,
    load_deployment_finalization,
)
from .post_deployment_assertion_observation import PostDeploymentAssertionPlan
from .state import StateError, StateStore, WorkItem


class CandidateFinalizationExecutionError(RuntimeError):
    """Finalization work could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateFinalizationExecutionResult:
    """Content-minimal result of one immutable finalization handoff."""

    deployment_id: str
    action: str
    outcome: str
    authority: str
    failure_stage: str
    candidate_blocked: bool
    replayed: bool
    completed_predicate_count: int
    work: WorkItem
    successor: WorkItem


def execute_candidate_finalization_once(
    store: StateStore,
    item: WorkItem,
    *,
    now: datetime | None = None,
) -> CandidateFinalizationExecutionResult:
    """Finalize trusted evidence and schedule exactly one inert successor."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_finalize"
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
        finalized = finalize_deployment_once(store, plan, finalized_at=when)
        action, successor_kind = _route(finalized)
        work, successor = _finish(store, item, when, plan, finalized, successor_kind)
        return CandidateFinalizationExecutionResult(
            item.work_key,
            action,
            finalized.outcome,
            finalized.authority,
            finalized.failure_stage,
            finalized.candidate_blocked,
            finalized.replayed,
            finalized.completed_predicate_count,
            work,
            successor,
        )
    except CandidateFinalizationExecutionError:
        raise
    except DeploymentFinalizationError as error:
        _reject(error.transient)
    except (CandidateAssertionObservationExecutionError, StateError, sqlite3.Error):
        _reject(False)


def _route(result: DeploymentFinalizationResult) -> tuple[str, str]:
    if (
        result.outcome == "success"
        and result.authority == "promote_and_tag"
        and result.failure_stage == "none"
        and result.candidate_blocked is False
    ):
        return "promotion_scheduled", "candidate_promote"
    if (
        result.outcome == "failure"
        and result.authority == "rollback"
        and result.failure_stage != "none"
        and result.candidate_blocked is True
    ):
        return "rollback_scheduled", "candidate_rollback"
    _reject(False)


def _finish(
    store: StateStore,
    item: WorkItem,
    when: datetime,
    plan: PostDeploymentAssertionPlan,
    result: DeploymentFinalizationResult,
    successor_kind: str,
) -> tuple[WorkItem, WorkItem]:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            current_plan = _load_plan(store, item.work_key)
            record = load_deployment_finalization(store, current_plan)
            if (
                current_plan != plan
                or record is None
                or record.outcome != result.outcome
                or record.failure_stage != result.failure_stage
                or record.completed_predicate_count != result.completed_predicate_count
            ):
                _reject(False)
            conflicts = db.execute(
                "SELECT COUNT(*) FROM work WHERE work_key=? "
                "AND work_kind IN ('candidate_promote','candidate_rollback')",
                (item.work_key,),
            ).fetchone()
            if conflicts is None or conflicts[0] != 0:
                _reject(False)
            current = when.isoformat()
            db.execute(
                "INSERT INTO work (work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at) VALUES (? ,?,'pending',0,?,?,?)",
                (successor_kind, item.work_key, current, current, current),
            )
            changed = db.execute(
                "UPDATE work SET status='succeeded',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate_finalize' AND work_key=? "
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
    except CandidateFinalizationExecutionError:
        raise
    except DeploymentFinalizationError as error:
        _reject(error.transient)
    except (CandidateAssertionObservationExecutionError, StateError, sqlite3.Error):
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidateFinalizationExecutionError(
        "Candidate deployment finalization failed closed", transient=transient
    ) from None
