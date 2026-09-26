"""Bounded recovery lane for interrupted deployment rollback work."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .automation_script_observation import derive_automation_script_target
from .deployment_rollback import (
    BackupReader,
    DeploymentRollback,
    DeploymentRollbackError,
    RepositoryReader,
    RollbackRestoreResult,
    complete_deployment_rollback_once,
    reconcile_deployment_restore_once,
    request_deployment_restore_once,
)
from .deployment_rollback_transport import (
    read_rollback_backup_proof,
    read_rollback_repository_proof,
)
from .post_deployment_assertion_observation import (
    PostDeploymentAssertionObservationError,
    PostDeploymentAssertionPlan,
    derive_post_deployment_assertion_plan,
)
from .resource_availability_observation import (
    ResourceAvailabilityError,
    ResourceAvailabilityTarget,
)
from .state import StateError, StateStore, WorkItem

_MAX_DISCOVERED_ROLLBACKS = 32
_WORK_KIND = "deployment_rollback"
_RECOVERY_AUTHORITY_SCHEMA_VERSION = 1
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


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


def _authority_digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def load_rollback_recovery_plan(
    store: StateStore, rollback: DeploymentRollback
) -> PostDeploymentAssertionPlan:
    """Reconstruct exact assertion authority from integrity-bound durable evidence."""
    try:
        rollback.validate()
        rows = store._connection.execute(
            "SELECT deployment_id, schema_version, candidate_sha, entity_ids_json, "
            "resource_target_sha256, automation_target_sha256, assertion_canonical_json, "
            "assertion_set_sha256, record_sha256 FROM rollback_recovery_authority "
            "WHERE deployment_id = ?",
            (rollback.deployment_id,),
        ).fetchall()
        if len(rows) != 1 or len(rows[0]) != 9:
            raise DeploymentRollbackRetriggerError("rollback recovery authority is unavailable")
        row = tuple(rows[0])
        (
            deployment_id,
            schema_version,
            candidate_sha,
            entity_ids_json,
            resource_target_sha256,
            automation_target_sha256,
            assertion_canonical_json,
            assertion_set_sha256,
            record_sha256,
        ) = row
        if (
            deployment_id != rollback.deployment_id
            or schema_version != _RECOVERY_AUTHORITY_SCHEMA_VERSION
            or candidate_sha != rollback.candidate_sha
            or not isinstance(candidate_sha, str)
            or _COMMIT.fullmatch(candidate_sha) is None
            or not all(
                isinstance(value, str)
                for value in (
                    entity_ids_json,
                    resource_target_sha256,
                    automation_target_sha256,
                    assertion_canonical_json,
                    assertion_set_sha256,
                    record_sha256,
                )
            )
            or any(
                _HASH.fullmatch(value) is None
                for value in (
                    resource_target_sha256,
                    automation_target_sha256,
                    assertion_set_sha256,
                    record_sha256,
                )
            )
            or record_sha256 != _authority_digest(row[:-1])
        ):
            raise DeploymentRollbackRetriggerError("rollback recovery authority is invalid")
        entities_value = json.loads(entity_ids_json)
        if (
            not isinstance(entities_value, list)
            or not all(isinstance(entity, str) for entity in entities_value)
            or json.dumps(entities_value, ensure_ascii=True, separators=(",", ":"))
            != entity_ids_json
        ):
            raise DeploymentRollbackRetriggerError("rollback recovery authority is invalid")
        prepared = store.prepared_deployment(rollback.deployment_id)
        if prepared is None or prepared.evidence.candidate_sha != rollback.candidate_sha:
            raise DeploymentRollbackRetriggerError("rollback recovery authority is invalid")
        resource_target = ResourceAvailabilityTarget.create(prepared, tuple(entities_value))
        if resource_target.target_sha256 != resource_target_sha256:
            raise DeploymentRollbackRetriggerError("rollback recovery authority is invalid")
        automation_target = derive_automation_script_target(resource_target)
        if automation_target.target_sha256 != automation_target_sha256:
            raise DeploymentRollbackRetriggerError("rollback recovery authority is invalid")
        plan = derive_post_deployment_assertion_plan(automation_target)
        if (
            plan.canonical_json != assertion_canonical_json
            or plan.assertion_set_sha256 != assertion_set_sha256
        ):
            raise DeploymentRollbackRetriggerError("rollback recovery authority is invalid")
        return plan
    except DeploymentRollbackRetriggerError:
        raise
    except (
        AttributeError,
        json.JSONDecodeError,
        PostDeploymentAssertionObservationError,
        ResourceAvailabilityError,
        sqlite3.Error,
        StateError,
        TypeError,
        ValueError,
    ):
        raise DeploymentRollbackRetriggerError("rollback recovery authority is invalid") from None


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


def _recover_interrupted_rollback_work(
    store: StateStore, deployment_ids: set[str], reference_time: datetime
) -> int:
    """Recover only running work still backed by a retryable rollback record."""
    if not deployment_ids:
        return 0
    when = reference_time.astimezone(UTC).isoformat()
    recovered = 0
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            for deployment_id in sorted(deployment_ids):
                result = db.execute(
                    "UPDATE work SET status = 'retry', updated_at = ?, next_attempt_at = ? "
                    "WHERE work_kind = ? AND status = 'running' AND work_key = ?",
                    (when, when, _WORK_KIND, deployment_id),
                )
                recovered += result.rowcount
        return recovered
    except sqlite3.Error:
        raise DeploymentRollbackRetriggerError("rollback work recovery is unavailable") from None


def _claim_discovered_work(
    store: StateStore,
    pending: list[DeploymentRollback],
    reference_time: datetime,
) -> tuple[int, WorkItem | None, DeploymentRollback | None]:
    """Persist deterministic identities, recover interrupted attempts, then claim one."""
    by_id = {rollback.deployment_id: rollback for rollback in pending}
    try:
        for deployment_id in sorted(by_id):
            store.enqueue_work(_WORK_KIND, deployment_id, now=reference_time)
        recovered = _recover_interrupted_rollback_work(store, set(by_id), reference_time)
        claimed = store.claim_work_kind(_WORK_KIND, now=reference_time)
    except StateError:
        raise DeploymentRollbackRetriggerError("rollback work claim is unavailable") from None
    if claimed is None:
        return recovered, None, None
    rollback = by_id.get(claimed.work_key)
    if rollback is None:
        store.fail_work(claimed, transient=False, now=reference_time)
        raise DeploymentRollbackRetriggerError("claimed rollback work has no durable authority")
    return recovered, claimed, rollback


def reconcile_pending_rollback(
    store: StateStore,
    rollback: DeploymentRollback,
    *,
    supervisor_token: str,
    observed_at: datetime,
) -> RollbackRestoreResult:
    """Reconcile one exact restore only after reconstructing its durable authority."""
    try:
        plan = load_rollback_recovery_plan(store, rollback)
        return reconcile_deployment_restore_once(
            store,
            plan,
            supervisor_token=supervisor_token,
            observed_at=observed_at,
        )
    except DeploymentRollbackRetriggerError:
        raise
    except DeploymentRollbackError:
        raise DeploymentRollbackRetriggerError("rollback reconciliation failed closed") from None


def execute_rollback_restore(
    store: StateStore,
    rollback: DeploymentRollback,
    *,
    github_token: str,
    supervisor_token: str,
    repository_reader: RepositoryReader,
    backup_reader: BackupReader,
    attempted_at: datetime,
) -> RollbackRestoreResult:
    """Request one exact restore through the existing journal-before-mutation guard."""
    try:
        plan = load_rollback_recovery_plan(store, rollback)
        return request_deployment_restore_once(
            store,
            plan,
            github_token=github_token,
            supervisor_token=supervisor_token,
            repository_reader=repository_reader,
            backup_reader=backup_reader,
            requested_at=attempted_at,
        )
    except DeploymentRollbackRetriggerError:
        raise
    except DeploymentRollbackError:
        raise DeploymentRollbackRetriggerError("rollback restore execution failed closed") from None


def complete_rollback_observation(
    store: StateStore,
    rollback: DeploymentRollback,
    *,
    github_token: str,
    supervisor_token: str,
    repository_reader: RepositoryReader,
    observed_at: datetime,
) -> RollbackRestoreResult:
    """Complete only the exact restored deployment after bounded health proofs."""
    try:
        plan = load_rollback_recovery_plan(store, rollback)
        return complete_deployment_rollback_once(
            store,
            plan,
            github_token=github_token,
            supervisor_token=supervisor_token,
            repository_reader=repository_reader,
            observed_at=observed_at,
        )
    except DeploymentRollbackRetriggerError:
        raise
    except DeploymentRollbackError:
        raise DeploymentRollbackRetriggerError(
            "rollback observation completion failed closed"
        ) from None


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
    repository_reader: RepositoryReader = read_rollback_repository_proof,
    backup_reader: BackupReader = read_rollback_backup_proof,
) -> DeploymentRollbackRetriggerResult:
    """Claim and process at most one rollback item without bypassing safeguards."""
    _validate_token(github_token, "GitHub token")
    _validate_token(core_token, "Core token")
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise DeploymentRollbackRetriggerError("reference_time must be timezone-aware")
    if not isinstance(target, str) or "/" not in target:
        raise DeploymentRollbackRetriggerError("invalid target")

    pending = list_retryable_rollbacks(store, reference_time)
    considered = len(pending)
    recovered, claimed, rollback = _claim_discovered_work(store, pending, reference_time)
    if claimed is None or rollback is None:
        return DeploymentRollbackRetriggerResult(recovered, considered, None)

    try:
        if rollback.phase == "observing":
            if reference_time < next_retry_at(rollback):
                store.fail_work(claimed, transient=True, now=reference_time)
                return DeploymentRollbackRetriggerResult(recovered, considered, None)
            # A restore has already been proved complete. Recovery may only finish
            # post-restore health observation; it must never issue another restore.
            complete_rollback_observation(
                store,
                rollback,
                github_token=github_token,
                supervisor_token=core_token,
                repository_reader=repository_reader,
                observed_at=reference_time,
            )
        elif rollback_requires_reconciliation(rollback):
            # Durable discovery must first bind this record back to the exact
            # PostDeploymentAssertionPlan. Until then this seam fails closed.
            reconcile_pending_rollback(
                store, rollback, supervisor_token=core_token, observed_at=reference_time
            )
        elif reference_time < next_retry_at(rollback):
            store.fail_work(claimed, transient=True, now=reference_time)
            return DeploymentRollbackRetriggerResult(recovered, considered, None)
        else:
            # Only a planned rollback can reach this path. The execution seam
            # remains fail-closed until persisted plan/proof reconstruction exists.
            execute_rollback_restore(
                store,
                rollback,
                github_token=github_token,
                supervisor_token=core_token,
                repository_reader=repository_reader,
                backup_reader=backup_reader,
                attempted_at=reference_time,
            )
    except DeploymentRollbackRetriggerError:
        store.fail_work(claimed, transient=True, now=reference_time)
        raise

    store.complete_work(claimed, now=reference_time)
    return DeploymentRollbackRetriggerResult(recovered, considered, rollback.deployment_id)
