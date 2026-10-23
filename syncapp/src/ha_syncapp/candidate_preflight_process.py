"""Pending-only normal processing for read-only candidate preflight actions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from .candidate_dependency_execution import (
    CandidateDependencyExecutionError,
    execute_candidate_dependency_once,
)
from .candidate_deploy_key_ingress import (
    DeployKeyCandidateIngress,
)
from .candidate_fetch_stage_execution import (
    CandidateFetchStageExecutionError,
    execute_candidate_fetch_stage_once,
)
from .candidate_integrity_execution import (
    CandidateIntegrityExecutionError,
    execute_candidate_integrity_once,
)
from .candidate_orchestration import (
    CandidateOrchestration,
    CandidateOrchestrationError,
    load_candidate_orchestration,
    register_claimed_candidate,
)
from .candidate_risk_execution import (
    CandidateRiskExecutionError,
    execute_candidate_risk_once,
)
from .candidate_semantic_execution import (
    CandidateSemanticExecutionError,
    execute_candidate_semantic_once,
)
from .candidate_static_execution import (
    CandidateStaticExecutionError,
    execute_candidate_static_once,
)
from .state import StateError, StateStore, WorkItem

CandidatePreflightAction = Literal[
    "fetch_stage",
    "analyze",
    "analyze_dependencies",
    "classify_risk",
    "validate",
    "validate_semantics",
]
CandidatePreflightOutcome = Literal["idle", "advanced", "retry", "blocked"]

_ACTION_PHASES: dict[str, str] = {
    "fetch_stage": "detected",
    "analyze": "staged",
    "analyze_dependencies": "integrity_verified",
    "classify_risk": "dependencies_analyzed",
    "validate": "risk_classified",
    "validate_semantics": "static_validated",
}
_ACTIONS = tuple(_ACTION_PHASES)


class CandidatePreflightProcessError(RuntimeError):
    """A routine candidate preflight claim or durable transition failed closed."""


@dataclass(frozen=True, slots=True)
class CandidatePreflightProcessResult:
    """Identity-free result from one bounded normal candidate action."""

    action: CandidatePreflightAction | None
    outcome: CandidatePreflightOutcome


Executor = Callable[..., object]
_EXECUTOR_ERRORS = (
    CandidateFetchStageExecutionError,
    CandidateIntegrityExecutionError,
    CandidateDependencyExecutionError,
    CandidateRiskExecutionError,
    CandidateStaticExecutionError,
    CandidateSemanticExecutionError,
)

_ExecutionError = (
    CandidateFetchStageExecutionError
    | CandidateIntegrityExecutionError
    | CandidateDependencyExecutionError
    | CandidateRiskExecutionError
    | CandidateStaticExecutionError
    | CandidateSemanticExecutionError
)


def run_candidate_preflight_process(
    store: StateStore,
    home_assistant_root: Path,
    fetch_workspace_root: Path,
    analysis_workspace_root: Path,
    staging_root: Path,
    target: str,
    github_token: str,
    *,
    deploy_key_ingress: DeployKeyCandidateIngress | None = None,
    core_token: str | None = None,
    reference_time: datetime | None = None,
    fetch_stage_executor: Executor = execute_candidate_fetch_stage_once,
    integrity_executor: Executor = execute_candidate_integrity_once,
    dependency_executor: Executor = execute_candidate_dependency_once,
    risk_executor: Executor = execute_candidate_risk_once,
    static_executor: Executor = execute_candidate_static_once,
    semantic_executor: Executor = execute_candidate_semantic_once,
) -> CandidatePreflightProcessResult:
    """Claim and execute at most one due, pending, read-only candidate action."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    if (
        type(store) is not StateStore
        or not isinstance(target, str)
        or not target
        or not isinstance(github_token, str)
        or not github_token
        or (
            deploy_key_ingress is not None
            and type(deploy_key_ingress) is not DeployKeyCandidateIngress
        )
        or not _aware(when)
        or any(
            not isinstance(path, Path) or not path.is_absolute()
            for path in (
                home_assistant_root,
                fetch_workspace_root,
                analysis_workspace_root,
                staging_root,
            )
        )
    ):
        raise CandidatePreflightProcessError("candidate preflight inputs are invalid")

    when = when.astimezone(UTC)
    try:
        repository_id = store.repository_id(target)
    except (StateError, sqlite3.Error):
        raise CandidatePreflightProcessError(
            "candidate preflight repository is unavailable"
        ) from None
    if type(repository_id) is not int or repository_id <= 0:
        raise CandidatePreflightProcessError("candidate preflight repository is not trusted")

    try:
        claimed, selected_action = _claim_due_pending_candidate(store, target, repository_id, when)
    except CandidatePreflightProcessError:
        raise
    except (CandidateOrchestrationError, StateError, sqlite3.Error, TypeError, ValueError):
        raise CandidatePreflightProcessError("candidate preflight claim failed closed") from None
    if claimed is None or selected_action is None:
        return CandidatePreflightProcessResult(action=None, outcome="idle")

    action = cast(CandidatePreflightAction, selected_action)
    try:
        orchestration = load_candidate_orchestration(store, claimed.work_key)
        if orchestration is None:
            if action != "fetch_stage":
                raise CandidatePreflightProcessError("candidate preflight authority is invalid")
            orchestration = register_claimed_candidate(
                store,
                claimed,
                target=target,
                repository_id=repository_id,
                now=when,
            )
        if (
            orchestration.target != target
            or orchestration.repository_id != repository_id
            or orchestration.candidate_sha != claimed.work_key
            or orchestration.next_action != action
            or _ACTION_PHASES.get(action) != orchestration.phase
        ):
            raise CandidatePreflightProcessError("candidate preflight authority is invalid")
    except CandidateOrchestrationError:
        return _record_executor_failure(store, claimed, action, None, when)
    except (StateError, sqlite3.Error, TypeError, ValueError):
        raise CandidatePreflightProcessError(
            "candidate preflight authority failed closed"
        ) from None

    try:
        _execute_action(
            action,
            store,
            orchestration,
            home_assistant_root=home_assistant_root,
            fetch_workspace_root=fetch_workspace_root,
            analysis_workspace_root=analysis_workspace_root,
            staging_root=staging_root,
            github_token=github_token,
            deploy_key_ingress=deploy_key_ingress,
            core_token=core_token,
            when=when,
            executors=(
                fetch_stage_executor,
                integrity_executor,
                dependency_executor,
                risk_executor,
                static_executor,
                semantic_executor,
            ),
        )
    except _EXECUTOR_ERRORS as error:
        return _record_executor_failure(store, claimed, action, error, when)
    except CandidateOrchestrationError:
        return _record_executor_failure(store, claimed, action, None, when)

    try:
        persisted = store._get_work(claimed.work_kind, claimed.work_key)
        if persisted.status == "blocked":
            return CandidatePreflightProcessResult(action, "blocked")
        if persisted != claimed:
            raise CandidatePreflightProcessError("candidate preflight outcome changed")
        deferred = store.defer_work(claimed, now=when)
    except CandidatePreflightProcessError:
        raise
    except (StateError, sqlite3.Error):
        raise CandidatePreflightProcessError("candidate preflight outcome failed closed") from None
    if deferred.status != "pending":
        raise CandidatePreflightProcessError("candidate preflight defer failed")
    return CandidatePreflightProcessResult(action, "advanced")


def _claim_due_pending_candidate(
    store: StateStore,
    target: str,
    repository_id: int,
    when: datetime,
) -> tuple[WorkItem | None, str | None]:
    current = when.isoformat()
    permitted = " OR ".join(
        f"(c.phase = '{phase}' AND c.next_action = '{action}')"
        for action, phase in _ACTION_PHASES.items()
    )
    try:
        with store._connection as database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                "SELECT w.work_kind,w.work_key,w.status,w.attempts,w.created_at,w.updated_at,"
                "w.next_attempt_at,c.phase,c.next_action FROM work AS w "
                "LEFT JOIN candidate_orchestration AS c "
                "ON c.candidate_sha=w.work_key WHERE w.work_kind='candidate' "
                "AND w.status='pending' AND w.attempts < ? AND w.next_attempt_at <= ? "
                "AND (c.candidate_sha IS NULL OR (c.target=? AND c.repository_id=? AND ("
                + permitted
                + "))) ORDER BY w.next_attempt_at,w.created_at,w.work_key LIMIT 1",
                (
                    StateStore.MAX_WORK_ATTEMPTS,
                    current,
                    target,
                    repository_id,
                ),
            ).fetchall()
            if not rows:
                return None, None

            row = tuple(rows[0])
            item = store._work_from_row(row[:7])
            selected_action = "fetch_stage" if row[8] is None else row[8]
            result = database.execute(
                "UPDATE work SET status='running',attempts=attempts+1,updated_at=?,"
                "next_attempt_at=NULL WHERE work_kind='candidate' AND work_key=? "
                "AND status='pending' AND attempts=? AND attempts < ? AND next_attempt_at=?",
                (
                    current,
                    item.work_key,
                    item.attempts,
                    StateStore.MAX_WORK_ATTEMPTS,
                    item.next_attempt_at.astimezone(UTC).isoformat()
                    if item.next_attempt_at is not None
                    else None,
                ),
            )
            if result.rowcount != 1:
                raise CandidatePreflightProcessError("candidate preflight claim changed")
            claimed = store._get_work(item.work_kind, item.work_key)
        return claimed, cast(str, selected_action)
    except CandidatePreflightProcessError:
        raise
    except (CandidateOrchestrationError, StateError, sqlite3.Error, TypeError, ValueError):
        raise CandidatePreflightProcessError("candidate preflight claim failed closed") from None


def _execute_action(
    action: CandidatePreflightAction,
    store: StateStore,
    orchestration: CandidateOrchestration,
    *,
    home_assistant_root: Path,
    fetch_workspace_root: Path,
    analysis_workspace_root: Path,
    staging_root: Path,
    github_token: str,
    deploy_key_ingress: DeployKeyCandidateIngress | None,
    core_token: str | None,
    when: datetime,
    executors: tuple[Executor, Executor, Executor, Executor, Executor, Executor],
) -> object:
    fetch_stage, integrity, dependencies, risk, static, semantic = executors
    if action == "fetch_stage":
        return fetch_stage(
            store,
            orchestration,
            token=None if deploy_key_ingress is not None else github_token,
            deploy_key_ingress=deploy_key_ingress,
            workspace_root=fetch_workspace_root,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            now=when,
        )
    if action == "analyze":
        return integrity(
            store,
            orchestration,
            token=github_token,
            workspace_root=analysis_workspace_root,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            now=when,
        )
    if action == "analyze_dependencies":
        return dependencies(
            store,
            orchestration,
            core_token=core_token,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            now=when,
        )
    if action == "classify_risk":
        return risk(store, orchestration, now=when)
    if action == "validate":
        return static(
            store,
            orchestration,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            now=when,
        )
    if action == "validate_semantics":
        return semantic(
            store,
            orchestration,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            now=when,
        )
    raise CandidatePreflightProcessError("candidate preflight action is invalid")


def _record_executor_failure(
    store: StateStore,
    claimed: WorkItem,
    action: CandidatePreflightAction,
    error: _ExecutionError | None,
    when: datetime,
) -> CandidatePreflightProcessResult:
    try:
        failed = store.fail_work(
            claimed,
            transient=False if error is None else error.transient,
            now=when,
        )
    except (StateError, sqlite3.Error):
        raise CandidatePreflightProcessError(
            "candidate preflight failure state is unavailable"
        ) from None
    return CandidatePreflightProcessResult(
        action,
        "retry" if failed.status == "retry" else "blocked",
    )


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )
