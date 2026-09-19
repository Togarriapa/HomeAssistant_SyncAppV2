"""Bounded transaction owner for one recovery-aware live Apply action."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from .apply_authorization import ApplyAuthorization
from .candidate_stage import CandidateStage
from .live_apply_intent import LiveApplyIntent, derive_live_apply_intent
from .live_apply_intent_store import load_live_apply_intent
from .live_apply_plan import LiveApplyPlan
from .live_apply_preconditions import LiveApplyPreconditionEvidence
from .live_apply_progress_store import discover_live_apply_recovery
from .live_apply_reconciliation import reconcile_live_apply_operation
from .live_apply_reconciliation_store import load_live_apply_reconciliation
from .live_apply_writer import apply_live_operation
from .stage_prewrite_reproof import StagePrewriteEvidence
from .state import StateError, StateStore


class LiveApplyControllerError(RuntimeError):
    """One bounded live Apply action could not be selected safely."""


@dataclass(frozen=True, slots=True)
class LiveApplyControllerResult:
    """Sanitized result of one bounded controller invocation."""

    action: str
    operation_index: int | None
    operation_path_sha256: str | None
    reconciliation_outcome: str | None = None
    replayed: bool = False


def advance_live_apply_once(
    store: StateStore,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
) -> LiveApplyControllerResult:
    """Advance exactly one journaled operation or one read-only reconciliation."""
    _validate_chain(store, authorization, stage_evidence, stage, plan, preconditions)
    try:
        decision = discover_live_apply_recovery(store, plan)
    except StateError:
        _reject("live Apply recovery state is invalid")

    if decision.action == "complete":
        if decision.operation_index is not None or decision.operation_path_sha256 is not None:
            _reject("live Apply completion state is invalid")
        return LiveApplyControllerResult("complete", None, None, replayed=True)

    operation_index = decision.operation_index
    operation_path_sha256 = decision.operation_path_sha256
    if (
        type(operation_index) is not int
        or not 0 <= operation_index < len(plan.operations)
        or not isinstance(operation_path_sha256, str)
    ):
        _reject("live Apply recovery decision is invalid")

    if decision.action == "start_next":
        try:
            writer_result = apply_live_operation(
                store,
                authorization,
                stage_evidence,
                stage,
                plan,
                preconditions,
                operation_index=operation_index,
            )
        except Exception:
            _reject("live Apply operation failed")
        return LiveApplyControllerResult(
            "operation_verified",
            writer_result.operation_index,
            writer_result.operation_path_sha256,
            replayed=writer_result.replayed,
        )

    if decision.action == "reconcile_uncertain":
        try:
            reconciliation_result = reconcile_live_apply_operation(
                store,
                authorization,
                stage_evidence,
                stage,
                plan,
                preconditions,
                operation_index=operation_index,
            )
        except Exception:
            _reject("live Apply reconciliation failed")
        action = "reconciled" if reconciliation_result.outcome == "applied" else "blocked"
        return LiveApplyControllerResult(
            action,
            reconciliation_result.operation_index,
            reconciliation_result.operation_path_sha256,
            reconciliation_outcome=reconciliation_result.outcome,
            replayed=reconciliation_result.replayed,
        )

    if decision.action == "blocked":
        try:
            outcome = load_live_apply_reconciliation(store, plan.deployment_id, operation_index)
        except StateError:
            _reject("live Apply blocked state is invalid")
        if outcome is not None and outcome.outcome not in {"not_applied", "ambiguous"}:
            _reject("live Apply blocked state is inconsistent")
        return LiveApplyControllerResult(
            "blocked",
            operation_index,
            operation_path_sha256,
            reconciliation_outcome=None if outcome is None else outcome.outcome,
            replayed=True,
        )

    _reject("live Apply recovery decision is invalid")


def _validate_chain(
    store: StateStore,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
) -> LiveApplyIntent:
    if type(store) is not StateStore or type(stage) is not CandidateStage:
        _reject("live Apply controller evidence is invalid")
    try:
        intent = derive_live_apply_intent(authorization, stage_evidence, plan, preconditions)
        persisted = load_live_apply_intent(store, intent.deployment_id)
    except Exception:
        _reject("live Apply controller evidence chain is invalid")
    if persisted is None:
        _reject("live Apply controller intent is missing")
    if (
        stage.target,
        stage.repository_id,
        stage.branch,
        stage.commit_sha,
        stage.manifest_sha256,
    ) != (
        intent.target,
        intent.repository_id,
        "candidate",
        intent.candidate_sha,
        intent.stage_manifest_sha256,
    ):
        _reject("live Apply controller Stage binding mismatch")
    if (
        persisted.deployment_id,
        persisted.target,
        persisted.repository_id,
        persisted.baseline_sha,
        persisted.candidate_sha,
        persisted.stage_manifest_sha256,
        persisted.backup_slug,
        persisted.homeassistant_root,
        persisted.operations_sha256,
    ) != (
        intent.deployment_id,
        intent.target,
        intent.repository_id,
        intent.baseline_sha,
        intent.candidate_sha,
        intent.stage_manifest_sha256,
        intent.backup_slug,
        intent.homeassistant_root,
        intent.operations_sha256,
    ):
        _reject("live Apply controller intent binding mismatch")
    return intent


def _reject(message: str) -> NoReturn:
    raise LiveApplyControllerError(message) from None
