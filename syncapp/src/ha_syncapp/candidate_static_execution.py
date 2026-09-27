"""Crash-safe execution of exact candidate static/configuration validation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from . import candidate_validation as validation_module
from .candidate_dependency_execution import (
    CandidateDependencyExecutionError,
    load_candidate_dependency_checkpoint,
)
from .candidate_fetch_stage_execution import (
    CandidateFetchStageExecutionError,
    load_candidate_fetch_stage_checkpoint,
    load_completed_candidate_stage,
)
from .candidate_integrity import CandidateIntegrity
from .candidate_integrity_execution import (
    CandidateIntegrityExecutionError,
    _integrity_from_checkpoint,
    load_candidate_integrity_checkpoint,
)
from .candidate_orchestration import (
    CandidateOrchestration,
    CandidateOrchestrationError,
    load_candidate_orchestration,
    static_blocked_candidate_orchestration,
    static_validated_candidate_orchestration,
)
from .candidate_risk_execution import (
    CandidateRiskExecutionError,
    load_candidate_risk_checkpoint,
)
from .candidate_static_checkpoint import (
    CandidateStaticCheckpoint,
    CandidateStaticCheckpointError,
)
from .candidate_validation import (
    CandidateStaticValidation,
    CandidateValidationError,
    validate_candidate_configuration,
)
from .state import StateError, StateStore

_MAX_DISCOVERABLE = 64


class CandidateStaticExecutionError(RuntimeError):
    """Candidate static validation could not proceed safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateStaticExecutionResult:
    checkpoint: CandidateStaticCheckpoint
    orchestration: CandidateOrchestration
    validation: CandidateStaticValidation
    replayed: bool


@dataclass(frozen=True, slots=True)
class CandidateStaticRuntimeEvidence:
    phase: str
    syntax_valid: bool | None
    invalid_count: int | None
    unvalidated_count: int | None
    planned_at: datetime
    completed_at: datetime | None


Validator = Callable[..., CandidateStaticValidation]


def execute_candidate_static_once(
    store: StateStore,
    orchestration: CandidateOrchestration,
    *,
    staging_root: Path,
    home_assistant_root: Path,
    validator: Validator = validate_candidate_configuration,
    now: datetime | None = None,
) -> CandidateStaticExecutionResult:
    when = datetime.now(UTC) if now is None else now
    try:
        current = load_candidate_orchestration(store, orchestration.candidate_sha)
        if current is None or current != orchestration:
            _invalid()
        fetch, stage = load_completed_candidate_stage(
            store,
            current,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
        )
        integrity_checkpoint = load_candidate_integrity_checkpoint(store, current.candidate_sha)
        dependency = load_candidate_dependency_checkpoint(store, current.candidate_sha)
        risk_checkpoint = load_candidate_risk_checkpoint(store, current.candidate_sha)
        if integrity_checkpoint is None or dependency is None or risk_checkpoint is None:
            _invalid()
        changes = integrity_checkpoint.changes()
        integrity: CandidateIntegrity = _integrity_from_checkpoint(integrity_checkpoint, changes)
        dependencies, runtime = dependency.dependencies(), dependency.runtime()
        impact = risk_checkpoint.impact(dependencies, runtime)
        risk = risk_checkpoint.risk(dependencies, runtime)
        checkpoint = load_candidate_static_checkpoint(store, current.candidate_sha)
        if checkpoint is not None and checkpoint.phase == "completed":
            if current.phase not in {"static_validated", "blocked"}:
                _invalid()
            result = checkpoint.validation()
            validation_module.verify_candidate_static_validation(
                result, integrity, stage, dependencies, impact, risk, runtime
            )
            return CandidateStaticExecutionResult(checkpoint, current, result, True)
        if current.phase != "risk_classified" or current.next_action != "validate":
            _invalid()
        if checkpoint is None:
            checkpoint = _record_plan(
                store,
                current,
                fetch.record_sha256,
                integrity_checkpoint.record_sha256,
                dependency.record_sha256,
                risk_checkpoint.record_sha256,
                integrity,
                when,
            )
        elif (
            checkpoint.orchestration_sha256 != current.record_sha256
            or checkpoint.fetch_stage_sha256 != fetch.record_sha256
            or checkpoint.integrity_sha256 != integrity_checkpoint.record_sha256
            or checkpoint.dependency_sha256 != dependency.record_sha256
            or checkpoint.risk_sha256 != risk_checkpoint.record_sha256
        ):
            _invalid()
        result = validator(integrity, stage, dependencies, impact, risk, runtime)
        completed = checkpoint.complete(
            result,
            integrity,
            stage,
            dependencies,
            impact,
            risk,
            runtime,
            completed_at=when,
        )
        advanced = (
            static_validated_candidate_orchestration(current, updated_at=when)
            if result.syntax_valid
            else static_blocked_candidate_orchestration(current, updated_at=when)
        )
        _complete_atomically(store, checkpoint, completed, current, advanced)
        return CandidateStaticExecutionResult(completed, advanced, result, False)
    except CandidateStaticExecutionError:
        raise
    except _EVIDENCE_ERRORS:
        _invalid()


def load_candidate_static_checkpoint(
    store: StateStore, candidate_sha: str
) -> CandidateStaticCheckpoint | None:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha,schema_version,orchestration_sha256,fetch_stage_sha256,"
            "integrity_sha256,dependency_sha256,risk_sha256,target,repository_id,baseline_sha,"
            "stage_manifest_sha256,phase,validation_json,syntax_valid,invalid_count,"
            "unvalidated_count,planned_at,completed_at,record_sha256 "
            "FROM candidate_static_checkpoint WHERE candidate_sha=?",
            (candidate_sha,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid()
        result = CandidateStaticCheckpoint.from_database_row(tuple(rows[0]))
        current = load_candidate_orchestration(store, candidate_sha)
        fetch = load_candidate_fetch_stage_checkpoint(store, candidate_sha)
        integrity = load_candidate_integrity_checkpoint(store, candidate_sha)
        dependency = load_candidate_dependency_checkpoint(store, candidate_sha)
        risk = load_candidate_risk_checkpoint(store, candidate_sha)
        if (
            current is None
            or fetch is None
            or integrity is None
            or dependency is None
            or risk is None
            or result.target != current.target
            or result.repository_id != current.repository_id
            or result.fetch_stage_sha256 != fetch.record_sha256
            or result.integrity_sha256 != integrity.record_sha256
            or result.dependency_sha256 != dependency.record_sha256
            or result.risk_sha256 != risk.record_sha256
            or (result.phase == "planned" and result.orchestration_sha256 != current.record_sha256)
            or (
                result.phase == "completed"
                and current.phase
                not in {"static_validated", "semantically_validated", "completed", "blocked"}
            )
        ):
            _invalid()
        return result
    except CandidateStaticExecutionError:
        raise
    except _EVIDENCE_ERRORS:
        _invalid()


def candidate_static_runtime_evidence(
    store: StateStore,
) -> tuple[CandidateStaticRuntimeEvidence, ...]:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha,schema_version,orchestration_sha256,fetch_stage_sha256,"
            "integrity_sha256,dependency_sha256,risk_sha256,target,repository_id,baseline_sha,"
            "stage_manifest_sha256,phase,validation_json,syntax_valid,invalid_count,"
            "unvalidated_count,planned_at,completed_at,record_sha256 "
            "FROM candidate_static_checkpoint ORDER BY planned_at,candidate_sha LIMIT ?",
            (_MAX_DISCOVERABLE + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE:
            _invalid()
        records = tuple(load_candidate_static_checkpoint(store, str(row[0])) for row in rows)
        if any(row is None for row in records):
            _invalid()
        return tuple(
            CandidateStaticRuntimeEvidence(
                row.phase,
                row.syntax_valid,
                row.invalid_count,
                row.unvalidated_count,
                row.planned_at,
                row.completed_at,
            )
            for row in records
            if row is not None
        )
    except CandidateStaticExecutionError:
        raise
    except _EVIDENCE_ERRORS:
        _invalid()


def _record_plan(
    store: StateStore,
    current: CandidateOrchestration,
    fetch_sha: str,
    integrity_sha: str,
    dependency_sha: str,
    risk_sha: str,
    integrity: CandidateIntegrity,
    when: datetime,
) -> CandidateStaticCheckpoint:
    plan = CandidateStaticCheckpoint.plan(
        candidate_sha=current.candidate_sha,
        orchestration_sha256=current.record_sha256,
        fetch_stage_sha256=fetch_sha,
        integrity_sha256=integrity_sha,
        dependency_sha256=dependency_sha,
        risk_sha256=risk_sha,
        target=current.target,
        repository_id=current.repository_id,
        baseline_sha=integrity.baseline_sha,
        stage_manifest_sha256=integrity.stage_manifest_sha256,
        planned_at=when,
    )
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if load_candidate_orchestration(store, current.candidate_sha) != current:
            _invalid()
        db.execute(
            "INSERT INTO candidate_static_checkpoint VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            plan.database_values(),
        )
    if load_candidate_static_checkpoint(store, current.candidate_sha) != plan:
        _invalid()
    return plan


def _complete_atomically(
    store: StateStore,
    planned: CandidateStaticCheckpoint,
    completed: CandidateStaticCheckpoint,
    current: CandidateOrchestration,
    advanced: CandidateOrchestration,
) -> None:
    if completed.completed_at is None:
        _invalid()
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            load_candidate_static_checkpoint(store, current.candidate_sha) != planned
            or load_candidate_orchestration(store, current.candidate_sha) != current
        ):
            _invalid()
        first = db.execute(
            "UPDATE candidate_static_checkpoint SET phase=?,validation_json=?,syntax_valid=?,"
            "invalid_count=?,unvalidated_count=?,completed_at=?,record_sha256=? "
            "WHERE candidate_sha=? AND phase='planned' AND record_sha256=?",
            (
                completed.phase,
                completed.validation_json,
                completed.syntax_valid,
                completed.invalid_count,
                completed.unvalidated_count,
                completed.completed_at.astimezone(UTC).isoformat(),
                completed.record_sha256,
                planned.candidate_sha,
                planned.record_sha256,
            ),
        )
        second = db.execute(
            "UPDATE candidate_orchestration SET phase=?,next_action=?,updated_at=?,record_sha256=? "
            "WHERE candidate_sha=? AND record_sha256=?",
            (
                advanced.phase,
                advanced.next_action,
                advanced.updated_at.astimezone(UTC).isoformat(),
                advanced.record_sha256,
                current.candidate_sha,
                current.record_sha256,
            ),
        )
        if not completed.syntax_valid:
            third = db.execute(
                "UPDATE work SET status='blocked',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate' AND work_key=? AND status='running'",
                (completed.completed_at.astimezone(UTC).isoformat(), current.candidate_sha),
            )
            if third.rowcount != 1:
                _invalid()
        if first.rowcount != 1 or second.rowcount != 1:
            _invalid()


def _invalid() -> NoReturn:
    raise CandidateStaticExecutionError("Candidate static evidence is invalid", transient=False)


_EVIDENCE_ERRORS = (
    CandidateDependencyExecutionError,
    CandidateFetchStageExecutionError,
    CandidateIntegrityExecutionError,
    CandidateOrchestrationError,
    CandidateRiskExecutionError,
    CandidateStaticCheckpointError,
    CandidateValidationError,
    StateError,
    sqlite3.Error,
    OSError,
    TypeError,
    ValueError,
)
