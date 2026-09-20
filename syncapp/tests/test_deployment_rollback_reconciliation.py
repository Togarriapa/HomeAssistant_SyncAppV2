"""Contract for read-only reconciliation of uncertain deployment rollback."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ha_syncapp.deployment_rollback import DeploymentRollback, DeploymentRollbackError
from ha_syncapp.deployment_rollback_reconciliation import (
    RollbackReconciliationProof,
    reconcile_deployment_rollback_once,
)


def _uncertain() -> DeploymentRollback:
    now = datetime(2026, 9, 20, 13, 55, tzinfo=UTC)
    rollback = DeploymentRollback(
        deployment_id="dep-1234567890abcdef",
        target="owner/repository",
        repository_id=123,
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
        backup_slug="backup_123",
        finalization_sha256="c" * 64,
        repository_proof_sha256="d" * 64,
        backup_proof_sha256="e" * 64,
        phase="uncertain",
        reconciliation_state="ambiguous",
        block_reason="none",
        attempt_count=1,
        restore_job_id=None,
        authorized_at=now,
        updated_at=now,
        record_sha256="f" * 64,
    )
    rollback.validate()
    return rollback


def test_uncertain_reconciliation_is_read_only_and_ambiguity_stays_blocked() -> None:
    rollback = _uncertain()
    calls: list[str] = []

    def reader(_rollback: DeploymentRollback) -> RollbackReconciliationProof:
        calls.append("read")
        return RollbackReconciliationProof(
            restore_state="ambiguous",
            supervisor_healthy=True,
            homeassistant_healthy=True,
        )

    result = reconcile_deployment_rollback_once(rollback, evidence_reader=reader)

    assert calls == ["read"]
    assert result.status == "reconciliation_required"
    assert result.retry_restore is False
    assert result.rollback.reconciliation_state == "ambiguous"
    assert result.rollback.phase == "uncertain"


def test_only_authoritative_not_started_can_become_explicitly_retryable() -> None:
    rollback = _uncertain()

    result = reconcile_deployment_rollback_once(
        rollback,
        evidence_reader=lambda _rollback: RollbackReconciliationProof(
            restore_state="not_started",
            supervisor_healthy=True,
            homeassistant_healthy=True,
        ),
    )

    assert result.status == "not_started"
    assert result.retry_restore is True
    assert result.rollback.reconciliation_state == "not_started"
    assert result.rollback.phase == "uncertain"


def test_reconciliation_never_accepts_unbounded_or_unknown_restore_state() -> None:
    rollback = _uncertain()

    with pytest.raises(DeploymentRollbackError, match="reconciliation evidence is invalid"):
        reconcile_deployment_rollback_once(
            rollback,
            evidence_reader=lambda _rollback: RollbackReconciliationProof(
                restore_state="unknown",
                supervisor_healthy=True,
                homeassistant_healthy=True,
            ),
        )


def test_restored_requires_both_health_signals_before_success() -> None:
    rollback = _uncertain()

    result = reconcile_deployment_rollback_once(
        rollback,
        evidence_reader=lambda _rollback: RollbackReconciliationProof(
            restore_state="restored",
            supervisor_healthy=True,
            homeassistant_healthy=False,
        ),
    )

    assert result.status == "reconciliation_required"
    assert result.retry_restore is False
