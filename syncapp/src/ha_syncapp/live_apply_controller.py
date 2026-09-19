"""Bounded controller for one crash-safe live Apply transaction step."""

from __future__ import annotations

from dataclasses import dataclass

from .apply_authorization import ApplyAuthorization
from .candidate_stage import CandidateStage
from .live_apply_intent import derive_live_apply_intent
from .live_apply_intent_store import load_live_apply_intent
from .live_apply_plan import LiveApplyPlan
from .live_apply_preconditions import LiveApplyPreconditionEvidence
from .live_apply_progress_store import discover_live_apply_recovery
from .live_apply_reconciliation import reconcile_live_apply_operation
from .live_apply_reconciliation_store import load_live_apply_reconciliation
from .live_apply_writer import apply_live_operation
from .stage_prewrite_reproof import StagePrewriteEvidence
from .state import StateError, StateStore


@dataclass(frozen=True, slots=True)
class LiveApplyControllerResult:
    """Sanitized outcome from advancing at most one live Apply operation."""

    action: str
    operation_index: int | None
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
    """Advance exactly one safe Apply action, or report the durable terminal state."""
    _validate_exact_intent(store, authorization, stage_evidence, plan, preconditions)
    try:
        decision = discover_live_apply_recovery(store, plan)
    except StateError:
        raise StateError("Unable to discover bounded live Apply recovery state") from None

    if decision.action == "complete":
        return LiveApplyControllerResult("complete", None)

    if decision.action == "blocked":
        if decision.operation_index is None:
            raise StateError("Blocked live Apply recovery state has no operation")
        record = load_live_apply_reconciliation(store, plan.deployment_id, decision.operation_index)
        if record is None or record.outcome not in {"not_applied", "ambiguous"}:
            raise StateError("Blocked live Apply recovery state lacks reconciliation")
        return LiveApplyControllerResult(
            "blocked",
            decision.operation_index,
            reconciliation_outcome=record.outcome,
            replayed=True,
        )

    if decision.action == "reconcile_uncertain":
        if decision.operation_index is None:
            raise StateError("Uncertain live Apply recovery state has no operation")
        result = reconcile_live_apply_operation(
            store,
            authorization,
            stage_evidence,
            stage,
            plan,
            preconditions,
            operation_index=decision.operation_index,
        )
        action = "reconciled" if result.outcome == "applied" else "blocked"
        return LiveApplyControllerResult(
            action,
            result.operation_index,
            reconciliation_outcome=result.outcome,
            replayed=result.replayed,
        )

    if decision.action == "start_next":
        if decision.operation_index is None:
            raise StateError("Next live Apply recovery state has no operation")
        result = apply_live_operation(
            store,
            authorization,
            stage_evidence,
            stage,
            plan,
            preconditions,
            operation_index=decision.operation_index,
        )
        return LiveApplyControllerResult(
            "operation_verified",
            result.operation_index,
            replayed=result.replayed,
        )

    raise StateError("Unsupported bounded live Apply recovery action")


def _validate_exact_intent(
    store: StateStore,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
) -> None:
    if type(store) is not StateStore:
        raise StateError("Invalid bounded live Apply state store")
    try:
        intent = derive_live_apply_intent(authorization, stage_evidence, plan, preconditions)
        persisted = load_live_apply_intent(store, intent.deployment_id)
    except Exception:
        raise StateError("Bounded live Apply evidence chain is invalid") from None
    if persisted is None or (
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
        raise StateError("Bounded live Apply durable intent binding mismatch")
