"""Strict non-authoritative lifecycle evidence for one future live Apply operation."""

from __future__ import annotations

import re
from dataclasses import dataclass

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


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None
