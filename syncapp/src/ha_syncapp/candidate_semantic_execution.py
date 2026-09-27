"""Crash-safe execution of exact candidate semantic validation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from . import candidate_semantics as semantic_module
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
    semantic_blocked_candidate_orchestration,
    semantically_validated_candidate_orchestration,
)
from .candidate_risk_execution import (
    CandidateRiskExecutionError,
    load_candidate_risk_checkpoint,
)
from .candidate_semantic_checkpoint import (
    CandidateSemanticCheckpoint,
    CandidateSemanticCheckpointError,
)
from .candidate_semantics import (
    CandidateSemanticError,
    CandidateSemanticValidation,
    validate_candidate_semantics,
)
from .candidate_static_execution import (
    CandidateStaticExecutionError,
    load_candidate_static_checkpoint,
)
from .core_version_evidence import CoreVersionEvidenceError, bind_core_version
from .state import StateError, StateStore

_MAX_DISCOVERABLE = 64


class CandidateSemanticExecutionError(RuntimeError):
    """Candidate semantic validation could not proceed safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateSemanticExecutionResult:
    checkpoint: CandidateSemanticCheckpoint
    orchestration: CandidateOrchestration
    semantic: CandidateSemanticValidation | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class CandidateSemanticRuntimeEvidence:
    phase: str
    succeeded: bool | None
    planned_at: datetime
    completed_at: datetime | None


Validator = Callable[..., CandidateSemanticValidation]


def execute_candidate_semantic_once(
    store: StateStore,
    orchestration: CandidateOrchestration,
    *,
    staging_root: Path,
    home_assistant_root: Path,
    validator: Validator = validate_candidate_semantics,
    now: datetime | None = None,
) -> CandidateSemanticExecutionResult:
    """Perform or replay one candidate-bound semantic validation decision."""
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
        static_checkpoint = load_candidate_static_checkpoint(store, current.candidate_sha)
        if (
            integrity_checkpoint is None
            or dependency is None
            or risk_checkpoint is None
            or static_checkpoint is None
        ):
            _invalid()
        integrity: CandidateIntegrity = _integrity_from_checkpoint(
            integrity_checkpoint, integrity_checkpoint.changes()
        )
        dependencies, runtime = dependency.dependencies(), dependency.runtime()
        impact = risk_checkpoint.impact(dependencies, runtime)
        risk = risk_checkpoint.risk(dependencies, runtime)
        static = static_checkpoint.validation()
        version = bind_core_version(runtime)
        checkpoint = load_candidate_semantic_checkpoint(store, current.candidate_sha)
        if checkpoint is not None and checkpoint.phase == "completed":
            if current.phase != "semantically_validated":
                _invalid()
            semantic = checkpoint.semantic()
            semantic_module.verify_candidate_semantic_validation(
                semantic,
                static,
                integrity,
                stage,
                dependencies,
                impact,
                risk,
                runtime,
                version,
            )
            return CandidateSemanticExecutionResult(checkpoint, current, semantic, True)
        if checkpoint is not None and checkpoint.phase == "blocked":
            if current.phase != "blocked":
                _invalid()
            return CandidateSemanticExecutionResult(checkpoint, current, None, True)
        if current.phase != "static_validated" or current.next_action != "validate_semantics":
            _invalid()
        if checkpoint is None:
            checkpoint = _record_plan(
                store,
                current,
                fetch.record_sha256,
                integrity_checkpoint.record_sha256,
                dependency.record_sha256,
                risk_checkpoint.record_sha256,
                static_checkpoint.record_sha256,
                integrity,
                version.runtime_sha256,
                version.version,
                when,
            )
        elif (
            checkpoint.orchestration_sha256 != current.record_sha256
            or checkpoint.fetch_stage_sha256 != fetch.record_sha256
            or checkpoint.integrity_sha256 != integrity_checkpoint.record_sha256
            or checkpoint.dependency_sha256 != dependency.record_sha256
            or checkpoint.risk_sha256 != risk_checkpoint.record_sha256
            or checkpoint.static_sha256 != static_checkpoint.record_sha256
            or checkpoint.runtime_sha256 != version.runtime_sha256
            or checkpoint.core_version != version.version
        ):
            _invalid()
        try:
            semantic = validator(
                static, integrity, stage, dependencies, impact, risk, runtime, version
            )
            semantic_module.verify_candidate_semantic_validation(
                semantic,
                static,
                integrity,
                stage,
                dependencies,
                impact,
                risk,
                runtime,
                version,
            )
        except CandidateSemanticError as exc:
            if exc.transient:
                raise CandidateSemanticExecutionError(
                    "Candidate semantic validation is temporarily unavailable",
                    transient=True,
                ) from None
            blocked = checkpoint.block(completed_at=when)
            advanced = semantic_blocked_candidate_orchestration(current, updated_at=when)
            _finish_atomically(store, checkpoint, blocked, current, advanced)
            return CandidateSemanticExecutionResult(blocked, advanced, None, False)
        completed = checkpoint.complete(
            semantic,
            static,
            integrity,
            stage,
            dependencies,
            impact,
            risk,
            runtime,
            version,
            completed_at=when,
        )
        advanced = semantically_validated_candidate_orchestration(current, updated_at=when)
        _finish_atomically(store, checkpoint, completed, current, advanced)
        return CandidateSemanticExecutionResult(completed, advanced, semantic, False)
    except CandidateSemanticExecutionError:
        raise
    except _EVIDENCE_ERRORS:
        _invalid()


def load_candidate_semantic_checkpoint(
    store: StateStore, candidate_sha: str
) -> CandidateSemanticCheckpoint | None:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha,schema_version,orchestration_sha256,fetch_stage_sha256,"
            "integrity_sha256,dependency_sha256,risk_sha256,static_sha256,target,repository_id,"
            "baseline_sha,stage_manifest_sha256,runtime_sha256,core_version,phase,semantic_json,"
            "planned_at,completed_at,record_sha256 FROM candidate_semantic_checkpoint "
            "WHERE candidate_sha=?",
            (candidate_sha,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid()
        result = CandidateSemanticCheckpoint.from_database_row(tuple(rows[0]))
        current = load_candidate_orchestration(store, candidate_sha)
        fetch = load_candidate_fetch_stage_checkpoint(store, candidate_sha)
        integrity = load_candidate_integrity_checkpoint(store, candidate_sha)
        dependency = load_candidate_dependency_checkpoint(store, candidate_sha)
        risk = load_candidate_risk_checkpoint(store, candidate_sha)
        static = load_candidate_static_checkpoint(store, candidate_sha)
        if (
            current is None
            or fetch is None
            or integrity is None
            or dependency is None
            or risk is None
            or static is None
            or result.target != current.target
            or result.repository_id != current.repository_id
            or result.fetch_stage_sha256 != fetch.record_sha256
            or result.integrity_sha256 != integrity.record_sha256
            or result.dependency_sha256 != dependency.record_sha256
            or result.risk_sha256 != risk.record_sha256
            or result.static_sha256 != static.record_sha256
            or (result.phase == "planned" and result.orchestration_sha256 != current.record_sha256)
            or (
                result.phase == "completed"
                and current.phase not in {"semantically_validated", "completed", "blocked"}
            )
            or (result.phase == "blocked" and current.phase != "blocked")
        ):
            _invalid()
        return result
    except CandidateSemanticExecutionError:
        raise
    except _EVIDENCE_ERRORS:
        _invalid()


def candidate_semantic_runtime_evidence(
    store: StateStore,
) -> tuple[CandidateSemanticRuntimeEvidence, ...]:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha FROM candidate_semantic_checkpoint "
            "ORDER BY planned_at,candidate_sha LIMIT ?",
            (_MAX_DISCOVERABLE + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE:
            _invalid()
        records = tuple(load_candidate_semantic_checkpoint(store, str(row[0])) for row in rows)
        if any(row is None for row in records):
            _invalid()
        return tuple(
            CandidateSemanticRuntimeEvidence(
                row.phase,
                True if row.phase == "completed" else False if row.phase == "blocked" else None,
                row.planned_at,
                row.completed_at,
            )
            for row in records
            if row is not None
        )
    except CandidateSemanticExecutionError:
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
    static_sha: str,
    integrity: CandidateIntegrity,
    runtime_sha: str,
    core_version: str,
    when: datetime,
) -> CandidateSemanticCheckpoint:
    plan = CandidateSemanticCheckpoint.plan(
        candidate_sha=current.candidate_sha,
        orchestration_sha256=current.record_sha256,
        fetch_stage_sha256=fetch_sha,
        integrity_sha256=integrity_sha,
        dependency_sha256=dependency_sha,
        risk_sha256=risk_sha,
        static_sha256=static_sha,
        target=current.target,
        repository_id=current.repository_id,
        baseline_sha=integrity.baseline_sha,
        stage_manifest_sha256=integrity.stage_manifest_sha256,
        runtime_sha256=runtime_sha,
        core_version=core_version,
        planned_at=when,
    )
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if load_candidate_orchestration(store, current.candidate_sha) != current:
            _invalid()
        db.execute(
            "INSERT INTO candidate_semantic_checkpoint VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            plan.database_values(),
        )
    if load_candidate_semantic_checkpoint(store, current.candidate_sha) != plan:
        _invalid()
    return plan


def _finish_atomically(
    store: StateStore,
    planned: CandidateSemanticCheckpoint,
    finished: CandidateSemanticCheckpoint,
    current: CandidateOrchestration,
    advanced: CandidateOrchestration,
) -> None:
    if finished.completed_at is None:
        _invalid()
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            load_candidate_semantic_checkpoint(store, current.candidate_sha) != planned
            or load_candidate_orchestration(store, current.candidate_sha) != current
        ):
            _invalid()
        first = db.execute(
            "UPDATE candidate_semantic_checkpoint SET phase=?,semantic_json=?,completed_at=?,"
            "record_sha256=? WHERE candidate_sha=? AND phase='planned' AND record_sha256=?",
            (
                finished.phase,
                finished.semantic_json,
                finished.completed_at.astimezone(UTC).isoformat(),
                finished.record_sha256,
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
        if finished.phase == "blocked":
            third = db.execute(
                "UPDATE work SET status='blocked',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate' AND work_key=? AND status='running'",
                (finished.completed_at.astimezone(UTC).isoformat(), current.candidate_sha),
            )
            if third.rowcount != 1:
                _invalid()
        if first.rowcount != 1 or second.rowcount != 1:
            _invalid()


def _invalid() -> NoReturn:
    raise CandidateSemanticExecutionError("Candidate semantic evidence is invalid", transient=False)


_EVIDENCE_ERRORS = (
    CandidateDependencyExecutionError,
    CandidateFetchStageExecutionError,
    CandidateIntegrityExecutionError,
    CandidateOrchestrationError,
    CandidateRiskExecutionError,
    CandidateSemanticCheckpointError,
    CandidateSemanticError,
    CandidateStaticExecutionError,
    CoreVersionEvidenceError,
    StateError,
    sqlite3.Error,
    OSError,
    TypeError,
    ValueError,
)
