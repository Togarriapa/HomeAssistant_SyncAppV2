"""Authorized handoff from a failed candidate to rollback recovery."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_assertion_observation_execution import (
    CandidateAssertionObservationExecutionError,
    _load_plan,
)
from .deploy_key_rollback_authority import DeployKeyRollbackRepositoryAuthority
from .deployment_rollback import (
    BackupReader,
    DeploymentRollbackError,
    RepositoryReader,
    authorize_deployment_rollback_once,
    load_deployment_rollback,
)
from .deployment_rollback_retrigger import (
    DeploymentRollbackRetriggerError,
    load_rollback_recovery_plan,
)
from .post_deployment_assertion_observation import PostDeploymentAssertionPlan
from .state import StateError, StateStore, WorkItem


class CandidateRollbackExecutionError(RuntimeError):
    """Candidate rollback authorization could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateRollbackExecutionResult:
    """Identity-free result of an authorized rollback handoff."""

    deployment_id: str
    action: str
    status: str
    replayed: bool
    work: WorkItem


def execute_candidate_rollback_once(
    store: StateStore,
    item: WorkItem,
    *,
    github_token: str | None,
    supervisor_token: str | None,
    repository_reader: RepositoryReader | None,
    backup_reader: BackupReader,
    repository_authority: DeployKeyRollbackRepositoryAuthority | None = None,
    now: datetime | None = None,
) -> CandidateRollbackExecutionResult:
    """Re-prove rollback inputs, persist authority, then complete exact work."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_rollback"
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
        if repository_authority is None:
            authorized = authorize_deployment_rollback_once(
                store,
                plan,
                github_token=github_token,
                supervisor_token=supervisor_token,
                repository_reader=repository_reader,
                backup_reader=backup_reader,
                observed_at=when,
            )
        else:
            authorized = authorize_deployment_rollback_once(
                store,
                plan,
                github_token=github_token,
                supervisor_token=supervisor_token,
                repository_reader=repository_reader,
                repository_authority=repository_authority,
                backup_reader=backup_reader,
                observed_at=when,
            )
        if (
            authorized.intent.deployment_id != item.work_key
            or authorized.status != authorized.intent.phase
        ):
            _reject(False)
        work = _finish(store, item, when, plan)
        return CandidateRollbackExecutionResult(
            item.work_key,
            "rollback_authorized",
            authorized.status,
            authorized.replayed,
            work,
        )
    except CandidateRollbackExecutionError:
        raise
    except DeploymentRollbackError as error:
        _reject(_is_transient(error))
    except (
        CandidateAssertionObservationExecutionError,
        DeploymentRollbackRetriggerError,
        StateError,
        sqlite3.Error,
    ):
        _reject(False)


def _finish(
    store: StateStore,
    item: WorkItem,
    when: datetime,
    plan: PostDeploymentAssertionPlan,
) -> WorkItem:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            current_plan = _load_plan(store, item.work_key)
            intent = load_deployment_rollback(store, current_plan)
            if current_plan != plan or intent is None:
                _reject(False)
            recovered = load_rollback_recovery_plan(store, intent)
            if recovered != current_plan:
                _reject(False)
            changed = db.execute(
                "UPDATE work SET status='succeeded',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate_rollback' AND work_key=? "
                "AND status='running' AND attempts=?",
                (when.isoformat(), item.work_key, item.attempts),
            )
            if changed.rowcount != 1:
                _reject(False)
        work = store._get_work(item.work_kind, item.work_key)
        if work.status != "succeeded":
            _reject(False)
        return work
    except CandidateRollbackExecutionError:
        raise
    except DeploymentRollbackError as error:
        _reject(_is_transient(error))
    except (
        CandidateAssertionObservationExecutionError,
        DeploymentRollbackRetriggerError,
        StateError,
        sqlite3.Error,
    ):
        _reject(False)


def _is_transient(error: DeploymentRollbackError) -> bool:
    return str(error) in {
        "repository proof is unavailable",
        "backup proof is unavailable",
    }


def _reject(transient: bool) -> NoReturn:
    raise CandidateRollbackExecutionError(
        "Candidate rollback authorization failed closed", transient=transient
    ) from None
