"""Bounded normal-service orchestration for routine candidate detection."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .candidate_detection import (
    CandidateDetectionError,
    CandidateDetectionResult,
    detect_and_enqueue_trusted_candidate,
)
from .github_repo import RepositoryVerificationError
from .state import StateStore


class CandidateDetectionServiceError(RuntimeError):
    """Routine candidate detection failed closed."""


@dataclass(frozen=True, slots=True)
class CandidateDetectionTickResult:
    """Sanitized result from one bounded candidate-detection tick."""

    due: bool
    detection: CandidateDetectionResult | None


class CandidateDetectionService:
    """Observe and enqueue at most one trusted candidate head when due."""

    def __init__(
        self,
        store: StateStore,
        target: str,
        github_token: str,
        *,
        interval_seconds: float,
    ) -> None:
        if type(store) is not StateStore:
            raise CandidateDetectionServiceError("candidate service state store is invalid")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise CandidateDetectionServiceError("candidate service interval is invalid")
        if not isinstance(target, str) or not target:
            raise CandidateDetectionServiceError("candidate service target is invalid")
        if not isinstance(github_token, str) or not github_token:
            raise CandidateDetectionServiceError("candidate service credential is invalid")

        self._store = store
        self._target = target
        self._github_token = github_token
        self._interval_seconds = float(interval_seconds)
        self._next_due: float | None = None
        self._last_now: float | None = None
        self._stopped = False

    def start(self, now: float) -> None:
        """Arm routine detection without performing a remote observation immediately."""
        current = self._advance_clock(now)
        if self._next_due is not None or self._stopped:
            raise CandidateDetectionServiceError("candidate service is already started")
        self._next_due = current + self._interval_seconds

    def tick(self, now: float) -> CandidateDetectionTickResult:
        """Perform at most one trusted observation/enqueue transaction when due."""
        current = self._advance_clock(now)
        if self._next_due is None or self._stopped:
            raise CandidateDetectionServiceError("candidate service is not started")
        if current < self._next_due:
            return CandidateDetectionTickResult(due=False, detection=None)

        # Advance from the current observation so delayed owner-loop execution
        # coalesces elapsed intervals instead of replaying a catch-up queue.
        self._next_due = current + self._interval_seconds
        try:
            detection = detect_and_enqueue_trusted_candidate(
                self._store,
                self._target,
                self._github_token,
            )
        except (CandidateDetectionError, RepositoryVerificationError) as exc:
            raise CandidateDetectionServiceError("candidate service tick failed closed") from exc
        return CandidateDetectionTickResult(due=True, detection=detection)

    def stop(self) -> None:
        """Disarm future observations before durable-state ownership is released."""
        if self._next_due is None or self._stopped:
            raise CandidateDetectionServiceError("candidate service is not started")
        self._stopped = True
        self._next_due = None

    def _advance_clock(self, now: float) -> float:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            raise CandidateDetectionServiceError("candidate service clock is invalid")
        current = float(now)
        if self._last_now is not None and current < self._last_now:
            raise CandidateDetectionServiceError("candidate service clock moved backwards")
        self._last_now = current
        return current
