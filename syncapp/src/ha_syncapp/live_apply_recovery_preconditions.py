"""Recover the admitted live Apply chain without replaying obsolete baseline proofs."""

from __future__ import annotations

from typing import NoReturn

from .apply_authorization import ApplyAuthorization
from .live_apply_intent_store import load_live_apply_intent
from .live_apply_plan import LiveApplyPlan
from .live_apply_preconditions import LiveApplyPreconditionEvidence
from .live_apply_progress import live_apply_plan_operations_sha256
from .live_apply_progress_store import discover_live_apply_recovery
from .stage_prewrite_reproof import StagePrewriteEvidence
from .state import StateStore


class LiveApplyRecoveryPreconditionError(RuntimeError):
    """Durable admission could not safely restore the exact Apply chain binding."""


def recover_live_apply_precondition_evidence(
    store: StateStore,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    plan: LiveApplyPlan,
) -> LiveApplyPreconditionEvidence:
    """Restore admitted chain binding; the writer still re-proves the next live path.

    The original all-path baseline proof cannot be repeated after earlier operations have
    succeeded.  Its integrity-protected durable intent is therefore used only to restore
    that exact binding.  This function grants no filesystem mutation on its own: recovery
    state is revalidated here, and the writer performs fresh before-journal and
    after-journal proofs for the one selected operation.
    """
    if (
        type(store) is not StateStore
        or type(authorization) is not ApplyAuthorization
        or type(stage_evidence) is not StagePrewriteEvidence
        or type(plan) is not LiveApplyPlan
    ):
        _reject("live Apply recovery evidence is invalid")

    try:
        intent = load_live_apply_intent(store, plan.deployment_id)
        operations_sha256 = live_apply_plan_operations_sha256(plan)
        discover_live_apply_recovery(store, plan)
    except Exception:
        _reject("live Apply recovery state is invalid")
    if intent is None:
        _reject("live Apply recovery intent is missing")

    authority = (
        authorization.deployment_id,
        authorization.target,
        authorization.repository_id,
        authorization.baseline_sha,
        authorization.candidate_sha,
        authorization.stage_manifest_sha256,
        authorization.backup_slug,
    )
    durable = (
        intent.deployment_id,
        intent.target,
        intent.repository_id,
        intent.baseline_sha,
        intent.candidate_sha,
        intent.stage_manifest_sha256,
        intent.backup_slug,
    )
    if authority != durable:
        _reject("live Apply recovery authority does not match durable intent")

    stage_binding = (
        stage_evidence.deployment_id,
        stage_evidence.target,
        stage_evidence.repository_id,
        stage_evidence.candidate_sha,
        stage_evidence.stage_manifest_sha256,
    )
    plan_binding = (
        plan.deployment_id,
        plan.target,
        plan.repository_id,
        plan.candidate_sha,
        plan.stage_manifest_sha256,
    )
    if stage_binding != plan_binding or stage_binding != (
        authorization.deployment_id,
        authorization.target,
        authorization.repository_id,
        authorization.candidate_sha,
        authorization.stage_manifest_sha256,
    ):
        _reject("live Apply recovery Stage binding is invalid")
    if plan.baseline_sha != authorization.baseline_sha:
        _reject("live Apply recovery plan binding is invalid")
    if operations_sha256 != intent.operations_sha256:
        _reject("live Apply recovery plan does not match durable intent")

    evidence = object.__new__(LiveApplyPreconditionEvidence)
    values = {
        "deployment_id": intent.deployment_id,
        "target": intent.target,
        "repository_id": intent.repository_id,
        "baseline_sha": intent.baseline_sha,
        "candidate_sha": intent.candidate_sha,
        "stage_manifest_sha256": intent.stage_manifest_sha256,
        "root": intent.homeassistant_root,
        "verified_paths": tuple(operation.path for operation in plan.operations),
    }
    for name, value in values.items():
        object.__setattr__(evidence, name, value)
    return evidence


def _reject(message: str) -> NoReturn:
    raise LiveApplyRecoveryPreconditionError(message) from None
