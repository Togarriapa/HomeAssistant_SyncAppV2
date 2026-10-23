"""Bounded normal-service cadence for read-only candidate preflight."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .candidate_deploy_key_ingress import DeployKeyCandidateIngress
from .candidate_preflight_process import (
    CandidatePreflightAction,
    CandidatePreflightOutcome,
    CandidatePreflightProcessError,
    CandidatePreflightProcessResult,
    run_candidate_preflight_process,
)
from .state import StateStore

_INTERVAL_SECONDS = 1.0
_ACTIONS: frozenset[CandidatePreflightAction] = frozenset(
    {
        "fetch_stage",
        "analyze",
        "analyze_dependencies",
        "classify_risk",
        "validate",
        "validate_semantics",
    }
)
_OUTCOMES: frozenset[CandidatePreflightOutcome] = frozenset(
    {"idle", "advanced", "retry", "blocked"}
)


class CandidatePreflightServiceError(RuntimeError):
    """Routine candidate preflight failed closed with sanitized diagnostics."""


@dataclass(frozen=True, slots=True)
class CandidatePreflightTickResult:
    """Identity-free result from one routine preflight cadence check."""

    due: bool
    process: CandidatePreflightProcessResult | None


class CandidatePreflightService:
    """Run at most one pending-only preflight action on each fixed cadence."""

    def __init__(
        self,
        store: StateStore,
        home_assistant_root: Path,
        fetch_workspace_root: Path,
        analysis_workspace_root: Path,
        staging_root: Path,
        target: str,
        github_token: str,
        *,
        deploy_key_ingress: DeployKeyCandidateIngress | None = None,
        core_token: str | None = None,
    ) -> None:
        if type(store) is not StateStore:
            raise CandidatePreflightServiceError("candidate preflight state store is invalid")
        paths = (
            home_assistant_root,
            fetch_workspace_root,
            analysis_workspace_root,
            staging_root,
        )
        if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
            raise CandidatePreflightServiceError("candidate preflight paths are invalid")
        if not isinstance(target, str) or not target:
            raise CandidatePreflightServiceError("candidate preflight target is invalid")
        if not isinstance(github_token, str) or not github_token:
            raise CandidatePreflightServiceError("candidate preflight credential is invalid")
        if (
            deploy_key_ingress is not None
            and type(deploy_key_ingress) is not DeployKeyCandidateIngress
        ):
            raise CandidatePreflightServiceError("candidate preflight credential is invalid")
        if core_token is not None and (not isinstance(core_token, str) or not core_token):
            raise CandidatePreflightServiceError("candidate preflight credential is invalid")

        self._store = store
        self._home_assistant_root = home_assistant_root
        self._fetch_workspace_root = fetch_workspace_root
        self._analysis_workspace_root = analysis_workspace_root
        self._staging_root = staging_root
        self._target = target
        self._github_token = github_token
        self._deploy_key_ingress = deploy_key_ingress
        self._core_token = core_token
        self._next_due: float | None = None
        self._last_now: float | None = None
        self._stopped = False

    def start(self, now: float) -> None:
        """Arm preflight without claiming work immediately at service startup."""
        current = self._advance_clock(now)
        if self._next_due is not None or self._stopped:
            raise CandidatePreflightServiceError("candidate preflight service is already started")
        self._next_due = current + _INTERVAL_SECONDS

    def tick(self, now: float) -> CandidatePreflightTickResult:
        """Execute no more than one normal pending-only action when the cadence is due."""
        current = self._advance_clock(now)
        if self._next_due is None or self._stopped:
            raise CandidatePreflightServiceError("candidate preflight service is not started")
        if current < self._next_due:
            return CandidatePreflightTickResult(due=False, process=None)

        # Schedule from the observed tick time so delayed owner-loop work coalesces.
        self._next_due = current + _INTERVAL_SECONDS
        try:
            result = run_candidate_preflight_process(
                self._store,
                self._home_assistant_root,
                self._fetch_workspace_root,
                self._analysis_workspace_root,
                self._staging_root,
                self._target,
                self._github_token,
                deploy_key_ingress=self._deploy_key_ingress,
                core_token=self._core_token,
                reference_time=datetime.now(UTC),
            )
        except CandidatePreflightProcessError:
            raise CandidatePreflightServiceError(
                "candidate preflight service tick failed closed"
            ) from None

        if not isinstance(result, CandidatePreflightProcessResult):
            raise CandidatePreflightServiceError("candidate preflight service result is invalid")
        valid_action = result.action is None or (
            type(result.action) is str and result.action in _ACTIONS
        )
        valid_outcome = type(result.outcome) is str and result.outcome in _OUTCOMES
        if (
            not valid_action
            or not valid_outcome
            or ((result.action is None) != (result.outcome == "idle"))
        ):
            raise CandidatePreflightServiceError("candidate preflight service result is invalid")
        return CandidatePreflightTickResult(due=True, process=result)

    def stop(self) -> None:
        """Disarm cadence state before the owner releases durable state."""
        if self._next_due is None or self._stopped:
            raise CandidatePreflightServiceError("candidate preflight service is not started")
        self._stopped = True
        self._next_due = None

    def _advance_clock(self, now: float) -> float:
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise CandidatePreflightServiceError("candidate preflight service clock is invalid")
        try:
            current = float(now)
        except (OverflowError, ValueError):
            raise CandidatePreflightServiceError(
                "candidate preflight service clock is invalid"
            ) from None
        if not math.isfinite(current) or current < 0:
            raise CandidatePreflightServiceError("candidate preflight service clock is invalid")
        if self._last_now is not None and current < self._last_now:
            raise CandidatePreflightServiceError(
                "candidate preflight service clock moved backwards"
            )
        self._last_now = current
        return current
