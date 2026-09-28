"""Guarded handoff from failed candidate finalization to rollback recovery."""

from __future__ import annotations

from dataclasses import dataclass


class CandidateRollbackHandoffExecutionError(RuntimeError):
    """Candidate rollback handoff could not advance safely."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateRollbackHandoffExecutionResult:
    """Content-minimal outcome returned by the handoff boundary."""

    deployment_id: str
    action: str
    status: str
    replayed: bool


def execute_candidate_rollback_handoff_once(*args: object, **kwargs: object) -> object:
    """Authorize durable rollback recovery without performing a restore."""
    raise CandidateRollbackHandoffExecutionError(
        "candidate rollback handoff is not implemented"
    )
