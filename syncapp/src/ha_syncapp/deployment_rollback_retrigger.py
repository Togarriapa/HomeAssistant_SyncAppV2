"""Bounded recovery lane for interrupted deployment rollback work."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .deployment_rollback import DeploymentRollback
from .state import StateError, StateStore

_MAX_DISCOVERED_ROLLBACKS = 32


class DeploymentRollbackRetriggerError(RuntimeError):
    """Rollback recovery inputs or durable state are invalid."""


@dataclass(frozen=True, slots=True)
class DeploymentRollbackRetriggerResult:
    recovered_stale_locks: int
    considered: int
    processed: str | None


def _validate_token(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(c.isspace() for c in value)
    ):
        raise DeploymentRollbackRetriggerError(f"invalid {name}")
    return value


def list_retryable_rollbacks(
    store: StateStore, reference_time: datetime
) -> list[DeploymentRollback]:
    """Read bounded durable rollback work while excluding terminal records."""
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise DeploymentRollbackRetriggerError("reference_time must be timezone-aware")
    try:
        rows = store._connection.execute(
            "SELECT deployment_id, target, repository_id, baseline_sha, candidate_sha, "
            "backup_slug, finalization_sha256, repository_proof_sha256, "
            "backup_proof_sha256, phase, reconciliation_state, block_reason, "
            "attempt_count, restore_job_id, authorized_at, updated_at, record_sha256 "
            "FROM deployment_rollback WHERE phase IN "
            "('planned','restore_started','restore_acknowledged','uncertain','observing') "
            "ORDER BY updated_at, deployment_id LIMIT ?",
            (_MAX_DISCOVERED_ROLLBACKS,),
        ).fetchall()
        return [DeploymentRollback.from_database_row(tuple(row)) for row in rows]
    except (sqlite3.Error, StateError, ValueError, TypeError, AttributeError):
        raise DeploymentRollbackRetriggerError("rollback discovery is unavailable") from None


def reconcile_pending_rollback(*_args: object, **_kwargs: object) -> None:
    """Fail closed until durable discovery can reconstruct the authoritative assertion plan."""
    raise DeploymentRollbackRetriggerError("rollback reconciliation is not wired")


def execute_rollback_restore(*_args: object, **_kwargs: object) -> None:
    """Fail closed until durable discovery can reconstruct authoritative restore proof."""
    raise DeploymentRollbackRetriggerError("rollback restore execution is not wired")


def rollback_requires_reconciliation(rollback: DeploymentRollback) -> bool:
    """Uncertain or acknowledged mutations must be reconciled, never replayed."""
    return rollback.phase in {"restore_started", "restore_acknowledged", "uncertain"}


def next_retry_at(rollback: DeploymentRollback) -> datetime:
    """Apply bounded exponential backoff to transient rollback recovery."""
    delay_minutes = min(60, 2 ** min(rollback.attempt_count, 5))
    return rollback.updated_at.astimezone(UTC) + timedelta(minutes=delay_minutes)


def run_deployment_rollback_retrigger_pass(
    store: StateStore,
    target: str,
    github_token: str,
    core_token: str,
    *,
    reference_time: datetime,
) -> DeploymentRollbackRetriggerResult:
    """Process at most one rollback recovery item without bypassing rollback safeguards."""
    _validate_token(github_token, "GitHub token")
    _validate_token(core_token, "Core token")
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise DeploymentRollbackRetriggerError("reference_time must be timezone-aware")
    if not isinstance(target, str) or "/" not in target:
        raise DeploymentRollbackRetriggerError("invalid target")

    pending = list_retryable_rollbacks(store, reference_time)
    considered = len(pending)
    for rollback in pending[:1]:
        if rollback_requires_reconciliation(rollback):
            # Durable discovery must first bind this record back to the exact
            # PostDeploymentAssertionPlan. Until then this seam fails closed.
            reconcile_pending_rollback(
                store, rollback, supervisor_token=core_token, observed_at=reference_time
            )
            return DeploymentRollbackRetriggerResult(0, considered, rollback.deployment_id)
        if reference_time < next_retry_at(rollback):
            return DeploymentRollbackRetriggerResult(0, considered, None)
        # Fail closed until persisted plan/proof reconstruction makes execution safe.
        execute_rollback_restore(
            store, rollback, supervisor_token=core_token, attempted_at=reference_time
        )
        return DeploymentRollbackRetriggerResult(0, considered, rollback.deployment_id)

    return DeploymentRollbackRetriggerResult(0, considered, None)
