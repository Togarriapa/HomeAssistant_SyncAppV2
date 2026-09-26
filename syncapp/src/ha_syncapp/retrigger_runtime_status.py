"""Bounded, deterministic Retrigger status for generated runtime inventory."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta

from .runtime_inventory import RuntimeInventoryInput
from .state import (
    MAX_RECOVERY_WORK_EVIDENCE_ROWS,
    DeploymentRollbackRuntimeEvidence,
    RecoveryWorkEvidence,
    StateError,
    StateStore,
)

MAX_RECOVERY_EVIDENCE_ROWS = MAX_RECOVERY_WORK_EVIDENCE_ROWS
_WORK_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_STATUSES = ("blocked", "pending", "retry", "running", "succeeded")
_ROLLBACK_PHASES = (
    "blocked",
    "completed",
    "observing",
    "planned",
    "restore_acknowledged",
    "restore_started",
    "uncertain",
)
_RECONCILIATION_STATES = ("ambiguous", "in_progress", "none", "not_started", "restored")
_ROLLBACK_BLOCK_REASONS = (
    "ambiguous",
    "backup_invalid",
    "invalid_authority",
    "none",
    "repository_divergence",
    "restore_rejected",
)


class RetriggerRuntimeStatusError(RuntimeError):
    """Retrigger status could not be exposed safely."""


def collect_retrigger_runtime_inventory(
    store: StateStore,
    *,
    reference_time: datetime,
) -> RuntimeInventoryInput:
    """Collect a read-only runtime input from the owning protected StateStore."""

    if type(store) is not StateStore:
        raise RetriggerRuntimeStatusError("recovery state store is invalid")
    try:
        evidence = store.recovery_work_evidence()
        rollback_evidence = store.deployment_rollback_runtime_evidence()
    except StateError:
        raise RetriggerRuntimeStatusError("recovery work evidence is unavailable") from None
    recovery = render_retrigger_runtime_status(evidence, reference_time=reference_time)
    recovery["deployment_rollback"] = render_deployment_rollback_runtime_status(
        rollback_evidence,
        reference_time=reference_time,
    )
    return RuntimeInventoryInput(
        manifest={},
        analysis={"recovery": recovery},
    )


def render_deployment_rollback_runtime_status(
    evidence: Iterable[DeploymentRollbackRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate rollback phases without exposing deployment, candidate, or backup identity."""

    reference = _utc(reference_time, "rollback reference time is invalid")
    phases = {value: 0 for value in _ROLLBACK_PHASES}
    reconciliation = {value: 0 for value in _RECONCILIATION_STATES}
    block_reasons = {value: 0 for value in _ROLLBACK_BLOCK_REASONS}
    attempts_total = 0
    attempts_maximum = 0
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError("rollback runtime evidence exceeds the limit")
        if (
            type(row) is not DeploymentRollbackRuntimeEvidence
            or row.phase not in phases
            or row.reconciliation_state not in reconciliation
            or row.block_reason not in block_reasons
            or type(row.attempt_count) is not int
            or not 0 <= row.attempt_count <= 8
        ):
            raise RetriggerRuntimeStatusError("rollback runtime evidence is invalid")
        updated = _utc(row.updated_at, "rollback runtime evidence is invalid")
        if updated > reference:
            raise RetriggerRuntimeStatusError("rollback runtime evidence is invalid")
        total += 1
        phases[row.phase] += 1
        reconciliation[row.reconciliation_state] += 1
        block_reasons[row.block_reason] += 1
        attempts_total += row.attempt_count
        attempts_maximum = max(attempts_maximum, row.attempt_count)
        latest = updated if latest is None or updated > latest else latest
    return {
        "total": total,
        "phases": phases,
        "reconciliation": reconciliation,
        "block_reasons": block_reasons,
        "attempts": {"maximum": attempts_maximum, "total": attempts_total},
        "latest_updated_at": None if latest is None else latest.isoformat(),
    }


def render_retrigger_runtime_status(
    evidence: Iterable[RecoveryWorkEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Render deterministic aggregates without reading a clock or mutating state."""

    reference = _utc(reference_time, "recovery reference time is invalid")
    rows = _bounded_evidence(evidence)
    totals = _new_counts()
    attempts_total = 0
    attempts_maximum = 0
    ready = 0
    scheduled = 0
    next_attempt: datetime | None = None
    per_kind: dict[str, list[RecoveryWorkEvidence]] = {}

    for row in rows:
        _validate_evidence(row, reference)
        totals[row.status] += 1
        attempts_total += row.attempts
        attempts_maximum = max(attempts_maximum, row.attempts)
        if row.status in {"pending", "retry"} and row.next_attempt_at is not None:
            if row.next_attempt_at <= reference:
                ready += 1
            else:
                scheduled += 1
                if next_attempt is None or row.next_attempt_at < next_attempt:
                    next_attempt = row.next_attempt_at
        per_kind.setdefault(row.work_kind, []).append(row)

    return {
        "reference_time": reference.isoformat(),
        "total": len(rows),
        "statuses": totals,
        "attempts": {"maximum": attempts_maximum, "total": attempts_total},
        "ready": ready,
        "backoff": {
            "scheduled": scheduled,
            "next_attempt_at": None if next_attempt is None else next_attempt.isoformat(),
        },
        "kinds": [_kind_status(kind, per_kind[kind], reference) for kind in sorted(per_kind)],
    }


def _bounded_evidence(
    supplied: Iterable[RecoveryWorkEvidence],
) -> tuple[RecoveryWorkEvidence, ...]:
    rows: list[RecoveryWorkEvidence] = []
    for row in supplied:
        if len(rows) >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError("recovery work evidence exceeds the limit")
        rows.append(row)
    return tuple(rows)


def _validate_evidence(row: RecoveryWorkEvidence, reference: datetime) -> None:
    if (
        type(row) is not RecoveryWorkEvidence
        or not isinstance(row.work_kind, str)
        or _WORK_KIND.fullmatch(row.work_kind) is None
        or row.status not in _STATUSES
        or type(row.attempts) is not int
        or not 0 <= row.attempts <= StateStore.MAX_WORK_ATTEMPTS
    ):
        raise RetriggerRuntimeStatusError("recovery work evidence is invalid")
    created = _utc(row.created_at, "recovery work evidence is invalid")
    updated = _utc(row.updated_at, "recovery work evidence is invalid")
    if created > updated or updated > reference:
        raise RetriggerRuntimeStatusError("recovery work evidence is invalid")
    if row.status == "pending" and row.attempts != 0:
        raise RetriggerRuntimeStatusError("recovery work evidence is invalid")
    if row.status != "pending" and row.attempts < 1:
        raise RetriggerRuntimeStatusError("recovery work evidence is invalid")
    if row.status in {"pending", "retry"}:
        if row.next_attempt_at is None:
            raise RetriggerRuntimeStatusError("recovery work evidence is invalid")
        _utc(row.next_attempt_at, "recovery work evidence is invalid")
    elif row.next_attempt_at is not None:
        raise RetriggerRuntimeStatusError("recovery work evidence is invalid")


def _kind_status(
    kind: str,
    rows: list[RecoveryWorkEvidence],
    reference: datetime,
) -> dict[str, object]:
    counts = _new_counts()
    for row in rows:
        counts[row.status] += 1
    attempts = [row.attempts for row in rows]
    waiting = sorted(
        row.next_attempt_at
        for row in rows
        if row.status in {"pending", "retry"}
        and row.next_attempt_at is not None
        and row.next_attempt_at > reference
    )
    return {
        "kind": kind,
        "total": len(rows),
        "statuses": counts,
        "attempts": {"maximum": max(attempts, default=0), "total": sum(attempts)},
        "ready": sum(
            row.status in {"pending", "retry"}
            and row.next_attempt_at is not None
            and row.next_attempt_at <= reference
            for row in rows
        ),
        "backoff": {
            "scheduled": len(waiting),
            "next_attempt_at": None if not waiting else waiting[0].isoformat(),
        },
    }


def _new_counts() -> dict[str, int]:
    return {status: 0 for status in _STATUSES}


def _utc(value: object, message: str) -> datetime:
    if not isinstance(value, datetime):
        raise RetriggerRuntimeStatusError(message)
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise RetriggerRuntimeStatusError(message)
    return value
