"""Bounded execution of affected automation/script load observation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .automation_script_observation import (
    AutomationScriptObservationError,
    AutomationScriptTarget,
    derive_automation_script_target,
    load_automation_script_observation,
    observe_automation_scripts_once,
)
from .candidate_entity_observation_execution import (
    CandidateEntityObservationExecutionError,
)
from .candidate_entity_observation_execution import (
    _load_target as _load_resource_target,
)
from .integration_observation import SessionFactory
from .state import StateError, StateStore, WorkItem


class CandidateAutomationObservationExecutionError(RuntimeError):
    """Automation/script observation work could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateAutomationObservationExecutionResult:
    """Content-minimal outcome of one automation/script observation."""

    deployment_id: str
    action: str
    outcome: str
    replayed: bool
    expected_count: int
    loaded_count: int
    failed_count: int
    work: WorkItem
    successor: WorkItem


def execute_candidate_automation_observation_once(
    store: StateStore,
    item: WorkItem,
    *,
    token: str | None = None,
    session_factory: SessionFactory | None = None,
    now: datetime | None = None,
) -> CandidateAutomationObservationExecutionResult:
    """Observe exact affected automations/scripts and schedule one successor."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_observe_automation_scripts"
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
        observed = observe_automation_scripts_once(
            store,
            target,
            token=token,
            session_factory=session_factory,
            observed_at=when,
        )
        action = (
            "assertion_observation_scheduled"
            if observed.status == "loaded"
            else "finalization_scheduled"
        )
        work, successor = _finish(store, item, when, target, observed.status)
        return CandidateAutomationObservationExecutionResult(
            item.work_key,
            action,
            observed.status,
            observed.replayed,
            observed.expected_count,
            observed.loaded_count,
            observed.failed_count,
            work,
            successor,
        )
    except CandidateAutomationObservationExecutionError:
        raise
    except AutomationScriptObservationError as error:
        _reject(error.transient)
    except (CandidateEntityObservationExecutionError, StateError, sqlite3.Error):
        _reject(False)


def _load_target(store: StateStore, deployment_id: str) -> AutomationScriptTarget:
    try:
        return derive_automation_script_target(_load_resource_target(store, deployment_id))
    except CandidateEntityObservationExecutionError:
        _reject(False)


def _finish(
    store: StateStore,
    item: WorkItem,
    when: datetime,
    target: AutomationScriptTarget,
    outcome: str,
) -> tuple[WorkItem, WorkItem]:
    successor_kind = {
        "loaded": "candidate_observe_assertions",
        "load_failed": "candidate_finalize",
    }.get(outcome)
    if successor_kind is None:
        _reject(False)
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            current_target = _load_target(store, item.work_key)
            observation = load_automation_script_observation(store, current_target)
            if (
                current_target != target
                or observation is None
                or (
                    outcome == "loaded"
                    and (
                        observation.failed_count != 0
                        or observation.loaded_count != observation.expected_count
                    )
                )
                or (outcome == "load_failed" and observation.failed_count == 0)
            ):
                _reject(False)
            conflicts = db.execute(
                "SELECT COUNT(*) FROM work WHERE work_key=? AND work_kind IN "
                "('candidate_observe_assertions','candidate_finalize')",
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
                "WHERE work_kind='candidate_observe_automation_scripts' AND work_key=? "
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
    except CandidateAutomationObservationExecutionError:
        raise
    except (
        AutomationScriptObservationError,
        CandidateEntityObservationExecutionError,
        StateError,
        sqlite3.Error,
    ):
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidateAutomationObservationExecutionError(
        "Candidate automation observation failed closed",
        transient=transient,
    ) from None
