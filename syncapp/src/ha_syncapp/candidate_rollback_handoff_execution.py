"""Guarded handoff from failed candidate finalization to rollback recovery."""

from __future__ import annotations


class CandidateRollbackHandoffExecutionError(RuntimeError):
    """Candidate rollback handoff could not advance safely."""


def execute_candidate_rollback_handoff_once(*args: object, **kwargs: object) -> object:
    """Authorize durable rollback recovery without performing a restore."""
    raise CandidateRollbackHandoffExecutionError("candidate rollback handoff is not implemented")
