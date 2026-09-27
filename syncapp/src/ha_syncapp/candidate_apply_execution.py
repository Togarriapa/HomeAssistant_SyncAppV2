"""Bounded execution of one admitted candidate live Apply action."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from .apply_authorization import (
    ApplyAuthorization,
    ApplyAuthorizationError,
    authorize_candidate_apply,
)
from .candidate_backup import CandidateBackupError, reprove_prepared_candidate_backup
from .candidate_backup_execution import (
    CandidateBackupExecutionError,
    load_candidate_backup_authority,
)
from .candidate_integrity_execution import load_candidate_integrity_checkpoint
from .candidate_orchestration import load_candidate_orchestration
from .candidate_stage import CandidateStage
from .live_apply_controller import (
    LiveApplyControllerError,
    LiveApplyControllerResult,
    advance_live_apply_once,
)
from .live_apply_plan import LiveApplyPlan, LiveApplyPlanError, build_live_apply_plan
from .live_apply_preconditions import LiveApplyPreconditionEvidence
from .live_apply_progress_store import discover_live_apply_recovery
from .live_apply_recovery_preconditions import (
    LiveApplyRecoveryPreconditionError,
    recover_live_apply_precondition_evidence,
)
from .post_apply_activation import (
    PostApplyActivationError,
    authorize_post_apply_activation,
)
from .preapply_freshness import PreApplyFreshnessError, reprove_preapply_repo_heads
from .stage_prewrite_reproof import (
    StagePrewriteEvidence,
    StagePrewriteReproofError,
    reprove_stage_for_apply,
)
from .state import StateError, StateStore, WorkItem


class CandidateApplyExecutionError(RuntimeError):
    """An admitted candidate could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateApplyExecutionResult:
    """Content-minimal outcome of one bounded Apply execution action."""

    deployment_id: str
    action: str
    operation_index: int | None
    replayed: bool
    work: WorkItem


def execute_candidate_apply_once(
    store: StateStore,
    item: WorkItem,
    *,
    staging_root: Path,
    home_assistant_root: Path,
    github_token: str | None,
    supervisor_token: str | None,
    now: datetime | None = None,
) -> CandidateApplyExecutionResult:
    """Re-prove authority and run at most one live mutation or reconciliation action."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_apply_execute"
        or item.status != "running"
        or item.attempts < 1
        or not isinstance(staging_root, Path)
        or not staging_root.is_absolute()
        or not isinstance(home_assistant_root, Path)
        or not home_assistant_root.is_absolute()
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _reject(False)
    try:
        prepared = store.prepared_deployment(item.work_key)
        if prepared is None:
            _reject(False)
        current = load_candidate_orchestration(store, prepared.evidence.candidate_sha)
        if (
            current is None
            or current.phase != "completed"
            or current.next_action != "none"
            or current.target != prepared.evidence.target
            or current.repository_id != prepared.evidence.repository_id
        ):
            _reject(False)
        authority = load_candidate_backup_authority(
            store,
            current,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
        )
        integrity = load_candidate_integrity_checkpoint(store, current.candidate_sha)
        if integrity is None or integrity.phase != "completed":
            _reject(False)
        backup = reprove_prepared_candidate_backup(
            prepared,
            authority.semantic,
            authority.static,
            authority.integrity,
            authority.stage,
            authority.dependencies,
            authority.impact,
            authority.risk,
            authority.runtime,
            authority.version,
            token=supervisor_token,
        )
        freshness = reprove_preapply_repo_heads(prepared, backup, token=github_token)
        authorization = authorize_candidate_apply(prepared, backup, freshness)
        stage_evidence = reprove_stage_for_apply(authorization, authority.stage)
        plan = build_live_apply_plan(stage_evidence, authority.stage, integrity.changes())
        preconditions = recover_live_apply_precondition_evidence(
            store, authorization, stage_evidence, plan
        )
        if preconditions.root != str(home_assistant_root):
            _reject(False)

        try:
            result = advance_live_apply_once(
                store,
                authorization,
                stage_evidence,
                authority.stage,
                plan,
                preconditions,
            )
        except LiveApplyControllerError:
            decision = discover_live_apply_recovery(store, plan)
            _reject(decision.action == "reconcile_uncertain")
        return _finish(
            store,
            item,
            result,
            authorization,
            stage_evidence,
            authority.stage,
            plan,
            preconditions,
            when,
        )
    except CandidateApplyExecutionError:
        raise
    except (CandidateBackupExecutionError, CandidateBackupError) as error:
        _reject(error.transient)
    except PreApplyFreshnessError as error:
        _reject(error.transient)
    except (
        ApplyAuthorizationError,
        StagePrewriteReproofError,
        LiveApplyPlanError,
        LiveApplyRecoveryPreconditionError,
        PostApplyActivationError,
        StateError,
    ):
        _reject(False)
    except Exception:
        _reject(False)


def _finish(
    store: StateStore,
    item: WorkItem,
    result: LiveApplyControllerResult,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
    when: datetime,
) -> CandidateApplyExecutionResult:
    if result.action in {"operation_verified", "reconciled"}:
        work = store.defer_work(item, now=when)
    elif result.action == "blocked":
        work = store.fail_work(item, transient=False, now=when)
    elif result.action == "complete":
        activation = authorize_post_apply_activation(
            store,
            authorization,
            stage_evidence,
            stage,
            plan,
            preconditions,
            authorized_at=when,
        )
        if activation.action == "restart_core":
            if activation.authorization is None:
                _reject(False)
            successor = store.enqueue_work("candidate_restart", item.work_key, now=when)
            if successor.status not in {"pending", "running", "retry", "succeeded"}:
                _reject(False)
        elif activation.action != "no_activation_required":
            _reject(False)
        work = store.complete_work(item, now=when)
    else:
        _reject(False)
    return CandidateApplyExecutionResult(
        item.work_key,
        result.action,
        result.operation_index,
        result.replayed,
        work,
    )


def _reject(transient: bool) -> NoReturn:
    raise CandidateApplyExecutionError(
        "Candidate Apply execution failed closed",
        transient=transient,
    ) from None
