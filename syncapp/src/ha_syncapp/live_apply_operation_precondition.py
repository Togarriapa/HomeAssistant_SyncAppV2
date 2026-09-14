"""Fresh read-only proof for exactly one live Apply operation immediately before mutation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .live_apply_plan import LiveApplyOperation, LiveApplyPlan
from .live_apply_preconditions import (
    LiveApplyPreconditionError,
    prove_live_apply_preconditions,
)


class LiveApplyOperationPreconditionError(RuntimeError):
    """One live Apply operation no longer satisfies its exact baseline precondition."""


@dataclass(frozen=True, slots=True, init=False)
class LiveApplyOperationPreconditionEvidence:
    """Ephemeral proof that one exact live path was re-read against its baseline."""

    deployment_id: str
    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    root: str
    operation_index: int
    path: str
    baseline_mode: str | None
    baseline_object_id: str | None


def prove_live_apply_operation_precondition(
    plan: LiveApplyPlan,
    homeassistant_root: Path,
    *,
    operation_index: int,
) -> LiveApplyOperationPreconditionEvidence:
    """Re-read one exact path through the existing no-follow verifier."""
    if type(plan) is not LiveApplyPlan:
        _reject("Apply plan evidence is invalid")
    if type(operation_index) is not int or not 0 <= operation_index < len(plan.operations):
        _reject("operation index is invalid")
    operation = plan.operations[operation_index]
    if type(operation) is not LiveApplyOperation:
        _reject("Apply plan operation is invalid")

    snapshot = _binding(plan, operation)
    single = object.__new__(LiveApplyPlan)
    for name, value in {
        "deployment_id": plan.deployment_id,
        "target": plan.target,
        "repository_id": plan.repository_id,
        "baseline_sha": plan.baseline_sha,
        "candidate_sha": plan.candidate_sha,
        "stage_manifest_sha256": plan.stage_manifest_sha256,
        "operations": (operation,),
    }.items():
        object.__setattr__(single, name, value)

    try:
        proof = prove_live_apply_preconditions(single, homeassistant_root)
    except LiveApplyPreconditionError as error:
        raise LiveApplyOperationPreconditionError(str(error)) from None

    if _binding(plan, plan.operations[operation_index]) != snapshot:
        _reject("Apply plan changed during operation precondition proof")
    if proof.verified_paths != (operation.path,):
        _reject("operation precondition proof is invalid")

    evidence = object.__new__(LiveApplyOperationPreconditionEvidence)
    values = {
        "deployment_id": plan.deployment_id,
        "target": plan.target,
        "repository_id": plan.repository_id,
        "baseline_sha": plan.baseline_sha,
        "candidate_sha": plan.candidate_sha,
        "stage_manifest_sha256": plan.stage_manifest_sha256,
        "root": proof.root,
        "operation_index": operation_index,
        "path": operation.path,
        "baseline_mode": operation.baseline_mode,
        "baseline_object_id": operation.baseline_object_id,
    }
    for name, value in values.items():
        object.__setattr__(evidence, name, value)
    return evidence


def _binding(plan: LiveApplyPlan, operation: LiveApplyOperation) -> tuple[object, ...]:
    return (
        plan.deployment_id,
        plan.target,
        plan.repository_id,
        plan.baseline_sha,
        plan.candidate_sha,
        plan.stage_manifest_sha256,
        operation.path,
        operation.status,
        operation.baseline_mode,
        operation.baseline_object_id,
        operation.candidate_mode,
        operation.candidate_object_id,
        operation.staged_size,
        operation.staged_sha256,
    )


def _reject(message: str) -> NoReturn:
    raise LiveApplyOperationPreconditionError(message) from None
