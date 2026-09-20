"""Read-only reconciliation for uncertain deployment rollback outcomes.

This module deliberately has no Supervisor mutation transport.  It classifies
bounded authoritative evidence so recovery code can decide whether a restore
may be explicitly retried without ever blindly replaying an ambiguous POST.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from .deployment_rollback import DeploymentRollback, DeploymentRollbackError

_ALLOWED_RESTORE_STATES = frozenset({"not_started", "in_progress", "restored", "ambiguous"})


@dataclass(frozen=True, slots=True)
class RollbackReconciliationProof:
    """Bounded read-only evidence about a previously uncertain restore."""

    restore_state: str
    supervisor_healthy: bool
    homeassistant_healthy: bool

    def validate(self) -> None:
        if (
            self.restore_state not in _ALLOWED_RESTORE_STATES
            or type(self.supervisor_healthy) is not bool
            or type(self.homeassistant_healthy) is not bool
        ):
            raise DeploymentRollbackError("reconciliation evidence is invalid")


@dataclass(frozen=True, slots=True)
class RollbackReconciliationResult:
    status: str
    retry_restore: bool
    rollback: DeploymentRollback


ReconciliationEvidenceReader = Callable[[DeploymentRollback], RollbackReconciliationProof]


def reconcile_deployment_rollback_once(
    rollback: DeploymentRollback,
    *,
    evidence_reader: ReconciliationEvidenceReader,
) -> RollbackReconciliationResult:
    """Classify uncertain restore evidence without performing any mutation.

    Only authoritative ``not_started`` evidence can expose retry eligibility.
    Ambiguous, in-progress, or restored evidence never grants restore replay.
    The caller remains responsible for durably persisting a subsequent state
    transition under the deployment lock before taking any further action.
    """
    rollback.validate()
    if rollback.phase != "uncertain" or rollback.reconciliation_state != "ambiguous":
        raise DeploymentRollbackError("rollback is not awaiting reconciliation")

    try:
        proof = evidence_reader(rollback)
    except DeploymentRollbackError:
        raise
    except Exception:
        raise DeploymentRollbackError("reconciliation evidence is unavailable") from None
    if not isinstance(proof, RollbackReconciliationProof):
        raise DeploymentRollbackError("reconciliation evidence is invalid")
    proof.validate()

    reconciled = replace(rollback, reconciliation_state=proof.restore_state)

    if proof.restore_state == "not_started":
        return RollbackReconciliationResult("not_started", True, reconciled)
    if proof.restore_state == "restored":
        if not (proof.supervisor_healthy and proof.homeassistant_healthy):
            return RollbackReconciliationResult("reconciliation_required", False, reconciled)
        return RollbackReconciliationResult("restored", False, reconciled)
    return RollbackReconciliationResult("reconciliation_required", False, reconciled)
