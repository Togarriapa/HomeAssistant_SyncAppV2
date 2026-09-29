"""Bounded, deterministic Retrigger status for generated runtime inventory."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta

from .candidate_backup_execution import (
    CandidateBackupExecutionError,
    CandidateBackupRuntimeEvidence,
    candidate_backup_runtime_evidence,
)
from .candidate_dependency_execution import (
    CandidateDependencyExecutionError,
    CandidateDependencyRuntimeEvidence,
    candidate_dependency_runtime_evidence,
)
from .candidate_fetch_stage_execution import (
    CandidateFetchStageExecutionError,
    CandidateFetchStageRuntimeEvidence,
    candidate_fetch_stage_runtime_evidence,
)
from .candidate_integrity_execution import (
    CandidateIntegrityExecutionError,
    CandidateIntegrityRuntimeEvidence,
    candidate_integrity_runtime_evidence,
)
from .candidate_risk_execution import (
    CandidateRiskExecutionError,
    CandidateRiskRuntimeEvidence,
    candidate_risk_runtime_evidence,
)
from .candidate_semantic_execution import (
    CandidateSemanticExecutionError,
    CandidateSemanticRuntimeEvidence,
    candidate_semantic_runtime_evidence,
)
from .candidate_static_execution import (
    CandidateStaticExecutionError,
    CandidateStaticRuntimeEvidence,
    candidate_static_runtime_evidence,
)
from .runtime_inventory import RuntimeInventoryInput
from .state import (
    MAX_RECOVERY_WORK_EVIDENCE_ROWS,
    AdministrativeRetryRuntimeEvidence,
    DeploymentRollbackRuntimeEvidence,
    RecoveryWorkEvidence,
    RepoBInitializationRuntimeEvidence,
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
        administrative_retry_evidence = store.administrative_retry_runtime_evidence()
        initialization_evidence = store.repo_b_initialization_runtime_evidence()
        rollback_evidence = store.deployment_rollback_runtime_evidence()
        fetch_stage_evidence = candidate_fetch_stage_runtime_evidence(store)
        integrity_evidence = candidate_integrity_runtime_evidence(store)
        dependency_evidence = candidate_dependency_runtime_evidence(store)
        risk_evidence = candidate_risk_runtime_evidence(store)
        static_evidence = candidate_static_runtime_evidence(store)
        semantic_evidence = candidate_semantic_runtime_evidence(store)
        backup_evidence = candidate_backup_runtime_evidence(store)
    except (
        StateError,
        CandidateFetchStageExecutionError,
        CandidateIntegrityExecutionError,
        CandidateDependencyExecutionError,
        CandidateRiskExecutionError,
        CandidateStaticExecutionError,
        CandidateSemanticExecutionError,
        CandidateBackupExecutionError,
    ):
        raise RetriggerRuntimeStatusError("recovery work evidence is unavailable") from None
    recovery = render_retrigger_runtime_status(evidence, reference_time=reference_time)
    recovery["administrative_retry_requests"] = render_administrative_retry_runtime_status(
        administrative_retry_evidence,
        reference_time=reference_time,
    )
    recovery["repo_b_initialization"] = render_repo_b_initialization_runtime_status(
        initialization_evidence,
        reference_time=reference_time,
    )
    recovery["deployment_rollback"] = render_deployment_rollback_runtime_status(
        rollback_evidence,
        reference_time=reference_time,
    )
    recovery["candidate_fetch_stage"] = render_candidate_fetch_stage_runtime_status(
        fetch_stage_evidence,
        reference_time=reference_time,
    )
    recovery["candidate_integrity"] = render_candidate_integrity_runtime_status(
        integrity_evidence,
        reference_time=reference_time,
    )
    recovery["candidate_dependencies"] = render_candidate_dependency_runtime_status(
        dependency_evidence,
        reference_time=reference_time,
    )
    recovery["candidate_risk"] = render_candidate_risk_runtime_status(
        risk_evidence,
        reference_time=reference_time,
    )
    recovery["candidate_static"] = render_candidate_static_runtime_status(
        static_evidence,
        reference_time=reference_time,
    )
    recovery["candidate_semantic"] = render_candidate_semantic_runtime_status(
        semantic_evidence,
        reference_time=reference_time,
    )
    recovery["candidate_backup"] = render_candidate_backup_runtime_status(
        backup_evidence,
        reference_time=reference_time,
    )
    return RuntimeInventoryInput(
        manifest={},
        analysis={"recovery": recovery},
    )


def render_administrative_retry_runtime_status(
    evidence: Iterable[AdministrativeRetryRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate explicit retry outcomes without request or work identity."""

    reference = _utc(reference_time, "administrative retry reference time is invalid")
    outcomes = {"rejected": 0, "retried": 0}
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError(
                "administrative retry runtime evidence exceeds the limit"
            )
        if type(row) is not AdministrativeRetryRuntimeEvidence or row.outcome not in outcomes:
            raise RetriggerRuntimeStatusError("administrative retry runtime evidence is invalid")
        processed = _utc(
            row.processed_at,
            "administrative retry runtime evidence is invalid",
        )
        if processed > reference:
            raise RetriggerRuntimeStatusError("administrative retry runtime evidence is invalid")
        total += 1
        outcomes[row.outcome] += 1
        latest = processed if latest is None or processed > latest else latest
    return {
        "total": total,
        "outcomes": outcomes,
        "latest_processed_at": None if latest is None else latest.isoformat(),
    }


def render_repo_b_initialization_runtime_status(
    evidence: Iterable[RepoBInitializationRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate initialization authority state without repository or key identity."""

    reference = _utc(reference_time, "Repo B initialization reference time is invalid")
    phases = {"authorized": 0, "blocked": 0, "completed": 0}
    reasons = {
        "active_request": 0,
        "already_initialized": 0,
        "none": 0,
        "repository_not_empty": 0,
    }
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError(
                "Repo B initialization runtime evidence exceeds the limit"
            )
        if (
            type(row) is not RepoBInitializationRuntimeEvidence
            or row.phase not in phases
            or row.block_reason not in reasons
            or (row.phase in {"authorized", "completed"} and row.block_reason != "none")
            or (row.phase == "blocked" and row.block_reason == "none")
        ):
            raise RetriggerRuntimeStatusError("Repo B initialization runtime evidence is invalid")
        recorded = _utc(
            row.recorded_at,
            "Repo B initialization runtime evidence is invalid",
        )
        if recorded > reference:
            raise RetriggerRuntimeStatusError("Repo B initialization runtime evidence is invalid")
        total += 1
        phases[row.phase] += 1
        reasons[row.block_reason] += 1
        latest = recorded if latest is None or recorded > latest else latest
    return {
        "total": total,
        "phases": phases,
        "block_reasons": reasons,
        "latest_recorded_at": None if latest is None else latest.isoformat(),
    }


def render_candidate_fetch_stage_runtime_status(
    evidence: Iterable[CandidateFetchStageRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate Fetch/Stage checkpoints without exposing candidate or repository identity."""

    reference = _utc(reference_time, "candidate Fetch/Stage reference time is invalid")
    phases = {"completed": 0, "planned": 0}
    entries_total = 0
    bytes_total = 0
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError(
                "candidate Fetch/Stage runtime evidence exceeds the limit"
            )
        if type(row) is not CandidateFetchStageRuntimeEvidence or row.phase not in phases:
            raise RetriggerRuntimeStatusError("candidate Fetch/Stage runtime evidence is invalid")
        planned = _utc(
            row.planned_at,
            "candidate Fetch/Stage runtime evidence is invalid",
        )
        completed = (
            None
            if row.completed_at is None
            else _utc(
                row.completed_at,
                "candidate Fetch/Stage runtime evidence is invalid",
            )
        )
        is_completed = row.phase == "completed"
        if (
            planned > reference
            or is_completed != (completed is not None)
            or is_completed != (row.entry_count is not None)
            or is_completed != (row.total_bytes is not None)
            or (completed is not None and (completed < planned or completed > reference))
            or (row.entry_count is not None and row.entry_count < 0)
            or (row.total_bytes is not None and row.total_bytes < 0)
        ):
            raise RetriggerRuntimeStatusError("candidate Fetch/Stage runtime evidence is invalid")
        total += 1
        phases[row.phase] += 1
        entries_total += row.entry_count or 0
        bytes_total += row.total_bytes or 0
        observed = completed or planned
        latest = observed if latest is None or observed > latest else latest
    return {
        "total": total,
        "phases": phases,
        "staged": {"entries": entries_total, "bytes": bytes_total},
        "latest_updated_at": None if latest is None else latest.isoformat(),
    }


def render_candidate_integrity_runtime_status(
    evidence: Iterable[CandidateIntegrityRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate candidate analysis checkpoints without identity or changed paths."""
    reference = _utc(reference_time, "candidate integrity reference time is invalid")
    phases = {"completed": 0, "planned": 0}
    changed_total = 0
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError(
                "candidate integrity runtime evidence exceeds the limit"
            )
        if type(row) is not CandidateIntegrityRuntimeEvidence or row.phase not in phases:
            raise RetriggerRuntimeStatusError("candidate integrity runtime evidence is invalid")
        planned = _utc(row.planned_at, "candidate integrity runtime evidence is invalid")
        completed = (
            None
            if row.completed_at is None
            else _utc(row.completed_at, "candidate integrity runtime evidence is invalid")
        )
        is_completed = row.phase == "completed"
        if (
            planned > reference
            or is_completed != (completed is not None)
            or is_completed != (row.changed_count is not None)
            or (completed is not None and (completed < planned or completed > reference))
            or (row.changed_count is not None and row.changed_count < 0)
        ):
            raise RetriggerRuntimeStatusError("candidate integrity runtime evidence is invalid")
        total += 1
        phases[row.phase] += 1
        changed_total += row.changed_count or 0
        observed = completed or planned
        latest = observed if latest is None or observed > latest else latest
    return {
        "total": total,
        "phases": phases,
        "changed_paths": changed_total,
        "latest_updated_at": None if latest is None else latest.isoformat(),
    }


def render_candidate_dependency_runtime_status(
    evidence: Iterable[CandidateDependencyRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate dependency checkpoints without identity or candidate contents."""
    reference = _utc(reference_time, "candidate dependency reference time is invalid")
    phases = {"completed": 0, "planned": 0}
    references = 0
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError(
                "candidate dependency runtime evidence exceeds the limit"
            )
        if type(row) is not CandidateDependencyRuntimeEvidence or row.phase not in phases:
            raise RetriggerRuntimeStatusError("candidate dependency runtime evidence is invalid")
        planned = _utc(row.planned_at, "candidate dependency runtime evidence is invalid")
        completed = (
            None
            if row.completed_at is None
            else _utc(row.completed_at, "candidate dependency runtime evidence is invalid")
        )
        is_completed = row.phase == "completed"
        if (
            planned > reference
            or is_completed != (completed is not None)
            or is_completed != (row.reference_count is not None)
            or (completed is not None and (completed < planned or completed > reference))
            or (row.reference_count is not None and row.reference_count < 0)
        ):
            raise RetriggerRuntimeStatusError("candidate dependency runtime evidence is invalid")
        total += 1
        phases[row.phase] += 1
        references += row.reference_count or 0
        observed = completed or planned
        latest = observed if latest is None or observed > latest else latest
    return {
        "total": total,
        "phases": phases,
        "references": references,
        "latest_updated_at": None if latest is None else latest.isoformat(),
    }


def render_candidate_risk_runtime_status(
    evidence: Iterable[CandidateRiskRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate risk checkpoints without identities, paths, contents, or reasons."""
    reference = _utc(reference_time, "candidate risk reference time is invalid")
    phases = {"completed": 0, "planned": 0}
    levels = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    affected = 0
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError("candidate risk evidence exceeds the limit")
        if type(row) is not CandidateRiskRuntimeEvidence or row.phase not in phases:
            raise RetriggerRuntimeStatusError("candidate risk evidence is invalid")
        planned = _utc(row.planned_at, "candidate risk evidence is invalid")
        completed = (
            None
            if row.completed_at is None
            else _utc(row.completed_at, "candidate risk evidence is invalid")
        )
        done = row.phase == "completed"
        if (
            planned > reference
            or done != (completed is not None)
            or done != (row.risk_level is not None)
            or done != (row.affected_count is not None)
            or (row.risk_level is not None and row.risk_level not in levels)
            or (row.affected_count is not None and row.affected_count < 0)
            or (completed is not None and (completed < planned or completed > reference))
        ):
            raise RetriggerRuntimeStatusError("candidate risk evidence is invalid")
        total += 1
        phases[row.phase] += 1
        if row.risk_level is not None:
            levels[row.risk_level] += 1
        affected += row.affected_count or 0
        observed = completed or planned
        latest = observed if latest is None or observed > latest else latest
    return {
        "total": total,
        "phases": phases,
        "levels": levels,
        "affected_entities": affected,
        "latest_updated_at": None if latest is None else latest.isoformat(),
    }


def render_candidate_static_runtime_status(
    evidence: Iterable[CandidateStaticRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate static checkpoints without identities, paths, contents, or reasons."""
    reference = _utc(reference_time, "candidate static reference time is invalid")
    phases = {"completed": 0, "planned": 0}
    outcomes = {"valid": 0, "invalid": 0}
    invalid_paths = 0
    unvalidated_paths = 0
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError("candidate static evidence exceeds the limit")
        if type(row) is not CandidateStaticRuntimeEvidence or row.phase not in phases:
            raise RetriggerRuntimeStatusError("candidate static evidence is invalid")
        planned = _utc(row.planned_at, "candidate static evidence is invalid")
        completed = (
            None
            if row.completed_at is None
            else _utc(row.completed_at, "candidate static evidence is invalid")
        )
        done = row.phase == "completed"
        if (
            planned > reference
            or done != (completed is not None)
            or done != (row.syntax_valid is not None)
            or done != (row.invalid_count is not None)
            or done != (row.unvalidated_count is not None)
            or (row.invalid_count is not None and row.invalid_count < 0)
            or (row.unvalidated_count is not None and row.unvalidated_count < 0)
            or (completed is not None and (completed < planned or completed > reference))
        ):
            raise RetriggerRuntimeStatusError("candidate static evidence is invalid")
        total += 1
        phases[row.phase] += 1
        if row.syntax_valid is not None:
            outcomes["valid" if row.syntax_valid else "invalid"] += 1
        invalid_paths += row.invalid_count or 0
        unvalidated_paths += row.unvalidated_count or 0
        observed = completed or planned
        latest = observed if latest is None or observed > latest else latest
    return {
        "total": total,
        "phases": phases,
        "outcomes": outcomes,
        "invalid_paths": invalid_paths,
        "unvalidated_paths": unvalidated_paths,
        "latest_updated_at": None if latest is None else latest.isoformat(),
    }


def render_candidate_semantic_runtime_status(
    evidence: Iterable[CandidateSemanticRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate semantic checkpoints without identities, contents, or failure reasons."""
    reference = _utc(reference_time, "candidate semantic reference time is invalid")
    phases = {"blocked": 0, "completed": 0, "planned": 0}
    outcomes = {"blocked": 0, "succeeded": 0}
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError("candidate semantic evidence exceeds the limit")
        if type(row) is not CandidateSemanticRuntimeEvidence or row.phase not in phases:
            raise RetriggerRuntimeStatusError("candidate semantic evidence is invalid")
        planned = _utc(row.planned_at, "candidate semantic evidence is invalid")
        completed = (
            None
            if row.completed_at is None
            else _utc(row.completed_at, "candidate semantic evidence is invalid")
        )
        terminal = row.phase in {"blocked", "completed"}
        expected_success = True if row.phase == "completed" else False if terminal else None
        if (
            planned > reference
            or terminal != (completed is not None)
            or row.succeeded is not expected_success
            or (completed is not None and (completed < planned or completed > reference))
        ):
            raise RetriggerRuntimeStatusError("candidate semantic evidence is invalid")
        total += 1
        phases[row.phase] += 1
        if row.succeeded is not None:
            outcomes["succeeded" if row.succeeded else "blocked"] += 1
        observed = completed or planned
        latest = observed if latest is None or observed > latest else latest
    return {
        "total": total,
        "phases": phases,
        "outcomes": outcomes,
        "latest_updated_at": None if latest is None else latest.isoformat(),
    }


def render_candidate_backup_runtime_status(
    evidence: Iterable[CandidateBackupRuntimeEvidence],
    *,
    reference_time: datetime,
) -> dict[str, object]:
    """Aggregate backup checkpoints without identities or backup names."""
    reference = _utc(reference_time, "candidate backup reference time is invalid")
    phases = {
        "blocked": 0,
        "completed": 0,
        "mutation_started": 0,
        "planned": 0,
        "uncertain": 0,
    }
    outcomes = {"blocked": 0, "succeeded": 0}
    mutation_started = 0
    latest: datetime | None = None
    total = 0
    for row in evidence:
        if total >= MAX_RECOVERY_EVIDENCE_ROWS:
            raise RetriggerRuntimeStatusError("candidate backup evidence exceeds the limit")
        if type(row) is not CandidateBackupRuntimeEvidence or row.phase not in phases:
            raise RetriggerRuntimeStatusError("candidate backup evidence is invalid")
        planned = _utc(row.planned_at, "candidate backup evidence is invalid")
        started = (
            None
            if row.started_at is None
            else _utc(row.started_at, "candidate backup evidence is invalid")
        )
        completed = (
            None
            if row.completed_at is None
            else _utc(row.completed_at, "candidate backup evidence is invalid")
        )
        terminal = row.phase in {"blocked", "completed"}
        expected_success = True if row.phase == "completed" else False if terminal else None
        expected_started = row.phase != "planned"
        if (
            planned > reference
            or row.mutation_started is not expected_started
            or expected_started != (started is not None)
            or terminal != (completed is not None)
            or row.succeeded is not expected_success
            or (started is not None and (started < planned or started > reference))
            or (
                completed is not None
                and (started is None or completed < started or completed > reference)
            )
        ):
            raise RetriggerRuntimeStatusError("candidate backup evidence is invalid")
        total += 1
        phases[row.phase] += 1
        mutation_started += int(row.mutation_started)
        if row.succeeded is not None:
            outcomes["succeeded" if row.succeeded else "blocked"] += 1
        observed = completed or started or planned
        latest = observed if latest is None or observed > latest else latest
    return {
        "total": total,
        "phases": phases,
        "outcomes": outcomes,
        "mutation_started": mutation_started,
        "latest_updated_at": None if latest is None else latest.isoformat(),
    }


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
