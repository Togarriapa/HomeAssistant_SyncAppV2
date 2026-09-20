"""Bounded recovery lane for interrupted deployment rollback work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .deployment_rollback import (
    DeploymentRollback,
    execute_deployment_restore_once as execute_rollback_restore,
    reconcile_deployment_restore_once as reconcile_pending_rollback,
)
from .state import StateStore


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
    """Return bounded retryable rollback records; wired to durable discovery in the next slice."""
    del store, reference_time
    return []


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
            reconcile_pending_rollback(
                store, rollback, supervisor_token=core_token, observed_at=reference_time
            )  # type: ignore[arg-type]
            return DeploymentRollbackRetriggerResult(0, considered, rollback.deployment_id)
        if reference_time < next_retry_at(rollback):
            return DeploymentRollbackRetriggerResult(0, considered, None)
        execute_rollback_restore(
            store, rollback, supervisor_token=core_token, attempted_at=reference_time
        )  # type: ignore[arg-type]
        return DeploymentRollbackRetriggerResult(0, considered, rollback.deployment_id)

    return DeploymentRollbackRetriggerResult(0, considered, None)
