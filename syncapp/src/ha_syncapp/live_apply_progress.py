"""Strict non-authoritative lifecycle evidence for one future live Apply operation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .live_apply_intent import LiveApplyIntent
from .live_apply_plan import LiveApplyOperation, LiveApplyPlan

_HASH = re.compile(r"^[0-9a-f]{64}$")
_PHASES = {"mutation_started", "mutation_verified", "blocked"}
_ALLOWED_TRANSITIONS = {
    "mutation_started": {"mutation_started", "mutation_verified", "blocked"},
    "mutation_verified": {"mutation_verified"},
    "blocked": {"blocked"},
}


class LiveApplyProgressError(RuntimeError):
    """Live Apply progress evidence is malformed or attempts an unsafe transition."""


@dataclass(frozen=True, slots=True, init=False)
class LiveApplyProgress:
    """Content-free immutable lifecycle evidence for exactly one ordered operation."""

    deployment_id: str
    intent_record_sha256: str
    operations_sha256: str
    operation_index: int
    operation_path_sha256: str
    phase: str

    @classmethod
    def create(
        cls,
        *,
        deployment_id: str,
        intent_record_sha256: str,
        operations_sha256: str,
        operation_index: int,
        operation_path_sha256: str,
        phase: str,
    ) -> LiveApplyProgress:
        """Create validated progress evidence without granting filesystem authority."""
        if (
            not isinstance(deployment_id, str)
            or not 1 <= len(deployment_id) <= 128
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in deployment_id)
        ):
            raise LiveApplyProgressError("deployment identity is invalid")
        if not _valid_hash(intent_record_sha256):
            raise LiveApplyProgressError("intent record binding is invalid")
        if not _valid_hash(operations_sha256):
            raise LiveApplyProgressError("operations binding is invalid")
        if type(operation_index) is not int or operation_index < 0:
            raise LiveApplyProgressError("operation index is invalid")
        if not _valid_hash(operation_path_sha256):
            raise LiveApplyProgressError("operation path binding is invalid")
        if phase not in _PHASES:
            raise LiveApplyProgressError("phase is invalid")

        progress = object.__new__(cls)
        object.__setattr__(progress, "deployment_id", deployment_id)
        object.__setattr__(progress, "intent_record_sha256", intent_record_sha256)
        object.__setattr__(progress, "operations_sha256", operations_sha256)
        object.__setattr__(progress, "operation_index", operation_index)
        object.__setattr__(progress, "operation_path_sha256", operation_path_sha256)
        object.__setattr__(progress, "phase", phase)
        return progress


def start_live_apply_progress(
    intent: LiveApplyIntent,
    intent_record_sha256: str,
    plan: LiveApplyPlan,
    *,
    operation_index: int,
) -> LiveApplyProgress:
    """Issue progress only for an operation from the exact bound plan."""
    if type(intent) is not LiveApplyIntent:
        raise LiveApplyProgressError("live Apply intent is invalid")
    if not _valid_hash(intent_record_sha256):
        raise LiveApplyProgressError("intent record binding is invalid")
    if type(plan) is not LiveApplyPlan:
        raise LiveApplyProgressError("Apply plan evidence is invalid")
    if (
        plan.deployment_id,
        plan.target,
        plan.repository_id,
        plan.baseline_sha,
        plan.candidate_sha,
        plan.stage_manifest_sha256,
    ) != (
        intent.deployment_id,
        intent.target,
        intent.repository_id,
        intent.baseline_sha,
        intent.candidate_sha,
        intent.stage_manifest_sha256,
    ):
        raise LiveApplyProgressError("Apply plan binding does not match live Apply intent")

    operations = _snapshot_operations(plan.operations)
    operations_sha256 = hashlib.sha256(
        json.dumps(operations, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    if operations_sha256 != intent.operations_sha256:
        raise LiveApplyProgressError("operations binding does not match live Apply intent")
    if type(operation_index) is not int or not 0 <= operation_index < len(operations):
        raise LiveApplyProgressError("operation index is invalid")

    operation = plan.operations[operation_index]
    return LiveApplyProgress.create(
        deployment_id=intent.deployment_id,
        intent_record_sha256=intent_record_sha256,
        operations_sha256=operations_sha256,
        operation_index=operation_index,
        operation_path_sha256=hashlib.sha256(operation.path.encode("utf-8")).hexdigest(),
        phase="mutation_started",
    )


def transition_live_apply_progress(
    current: LiveApplyProgress,
    destination: str,
) -> LiveApplyProgress:
    """Apply one monotonic lifecycle transition; exact replay is idempotent."""
    if type(current) is not LiveApplyProgress:
        raise LiveApplyProgressError("progress evidence is invalid")
    if destination not in _PHASES:
        raise LiveApplyProgressError("phase is invalid")
    if destination not in _ALLOWED_TRANSITIONS[current.phase]:
        raise LiveApplyProgressError("progress transition is not permitted")
    if destination == current.phase:
        return current
    return LiveApplyProgress.create(
        deployment_id=current.deployment_id,
        intent_record_sha256=current.intent_record_sha256,
        operations_sha256=current.operations_sha256,
        operation_index=current.operation_index,
        operation_path_sha256=current.operation_path_sha256,
        phase=destination,
    )


def _snapshot_operations(operations: object) -> tuple[tuple[object, ...], ...]:
    if type(operations) is not tuple:
        raise LiveApplyProgressError("Apply plan operations are invalid")
    result: list[tuple[object, ...]] = []
    previous: bytes | None = None
    seen: set[str] = set()
    for operation in operations:
        if type(operation) is not LiveApplyOperation:
            raise LiveApplyProgressError("Apply plan operations are invalid")
        if not _safe_path(operation.path):
            raise LiveApplyProgressError("Apply plan operations are invalid")
        key = operation.path.encode("utf-8")
        if operation.path in seen or (previous is not None and key <= previous):
            raise LiveApplyProgressError("Apply plan operations are not deterministically ordered")
        previous = key
        seen.add(operation.path)
        result.append(
            (
                operation.path,
                operation.status,
                operation.baseline_mode,
                operation.baseline_object_id,
                operation.candidate_mode,
                operation.candidate_object_id,
                operation.staged_size,
                operation.staged_sha256,
            )
        )
    return tuple(result)


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    parts = value.split("/")
    return not any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts)


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None
