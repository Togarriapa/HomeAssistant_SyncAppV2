"""Crash-safe execution of exact candidate impact and risk classification."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_dependency_execution import (
    CandidateDependencyExecutionError,
    load_candidate_dependency_checkpoint,
)
from .candidate_impact import CandidateImpactAnalysis, CandidateImpactError, expand_candidate_impact
from .candidate_orchestration import (
    CandidateOrchestration,
    CandidateOrchestrationError,
    load_candidate_orchestration,
    risk_classified_candidate_orchestration,
)
from .candidate_risk import CandidateRiskClassification, CandidateRiskError, classify_candidate_risk
from .candidate_risk_checkpoint import CandidateRiskCheckpoint, CandidateRiskCheckpointError
from .state import StateError, StateStore

_MAX_DISCOVERABLE = 64


class CandidateRiskExecutionError(RuntimeError):
    """Candidate risk classification could not proceed safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateRiskExecutionResult:
    checkpoint: CandidateRiskCheckpoint
    orchestration: CandidateOrchestration
    impact: CandidateImpactAnalysis
    risk: CandidateRiskClassification
    replayed: bool


@dataclass(frozen=True, slots=True)
class CandidateRiskRuntimeEvidence:
    phase: str
    risk_level: str | None
    affected_count: int | None
    planned_at: datetime
    completed_at: datetime | None


ImpactExpander = Callable[..., CandidateImpactAnalysis]
RiskClassifier = Callable[..., CandidateRiskClassification]


def execute_candidate_risk_once(
    store: StateStore,
    orchestration: CandidateOrchestration,
    *,
    impact_expander: ImpactExpander = expand_candidate_impact,
    classifier: RiskClassifier = classify_candidate_risk,
    now: datetime | None = None,
) -> CandidateRiskExecutionResult:
    """Execute or replay one dependency-bound, read-only risk classification."""
    when = datetime.now(UTC) if now is None else now
    try:
        current = load_candidate_orchestration(store, orchestration.candidate_sha)
        if current is None or current != orchestration:
            _invalid()
        dependency = load_candidate_dependency_checkpoint(store, current.candidate_sha)
        if dependency is None or dependency.phase != "completed":
            _invalid()
        dependencies, runtime = dependency.dependencies(), dependency.runtime()
        checkpoint = load_candidate_risk_checkpoint(store, current.candidate_sha)
        if checkpoint is not None and checkpoint.phase == "completed":
            if current.phase != "risk_classified" or current.next_action != "validate":
                _invalid()
            return CandidateRiskExecutionResult(
                checkpoint,
                current,
                checkpoint.impact(dependencies, runtime),
                checkpoint.risk(dependencies, runtime),
                True,
            )
        if current.phase != "dependencies_analyzed" or current.next_action != "classify_risk":
            _invalid()
        if checkpoint is None:
            checkpoint = _record_plan(store, current, dependency, when)
        elif (
            checkpoint.orchestration_sha256 != current.record_sha256
            or checkpoint.dependency_sha256 != dependency.record_sha256
        ):
            _invalid()
        impact = impact_expander(dependencies, runtime)
        risk = classifier(dependencies, impact, runtime)
        completed = checkpoint.complete(impact, risk, dependencies, runtime, completed_at=when)
        advanced = risk_classified_candidate_orchestration(current, updated_at=when)
        _complete_atomically(store, checkpoint, completed, current, advanced)
        return CandidateRiskExecutionResult(completed, advanced, impact, risk, False)
    except CandidateRiskExecutionError:
        raise
    except (
        CandidateDependencyExecutionError,
        CandidateImpactError,
        CandidateOrchestrationError,
        CandidateRiskCheckpointError,
        CandidateRiskError,
        StateError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        _invalid()


def load_candidate_risk_checkpoint(
    store: StateStore, candidate_sha: str
) -> CandidateRiskCheckpoint | None:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha,schema_version,orchestration_sha256,dependency_sha256,"
            "target,repository_id,baseline_sha,stage_manifest_sha256,phase,impact_json,"
            "risk_json,risk_level,affected_count,planned_at,completed_at,record_sha256 "
            "FROM candidate_risk_checkpoint WHERE candidate_sha = ?",
            (candidate_sha,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid()
        result = CandidateRiskCheckpoint.from_database_row(tuple(rows[0]))
        current = load_candidate_orchestration(store, candidate_sha)
        dependency = load_candidate_dependency_checkpoint(store, candidate_sha)
        if (
            current is None
            or dependency is None
            or result.dependency_sha256 != dependency.record_sha256
            or result.target != current.target
            or result.repository_id != current.repository_id
            or (result.phase == "planned" and result.orchestration_sha256 != current.record_sha256)
            or (
                result.phase == "completed"
                and current.phase not in {"risk_classified", "static_validated", "blocked"}
            )
        ):
            _invalid()
        if result.phase == "completed":
            result.validate(dependency.dependencies(), dependency.runtime())
        return result
    except CandidateRiskExecutionError:
        raise
    except (
        CandidateDependencyExecutionError,
        CandidateOrchestrationError,
        CandidateRiskCheckpointError,
        StateError,
        sqlite3.Error,
    ):
        _invalid()


def candidate_risk_runtime_evidence(store: StateStore) -> tuple[CandidateRiskRuntimeEvidence, ...]:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha,schema_version,orchestration_sha256,dependency_sha256,"
            "target,repository_id,baseline_sha,stage_manifest_sha256,phase,impact_json,"
            "risk_json,risk_level,affected_count,planned_at,completed_at,record_sha256 "
            "FROM candidate_risk_checkpoint ORDER BY planned_at,candidate_sha LIMIT ?",
            (_MAX_DISCOVERABLE + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE:
            _invalid()
        records = tuple(CandidateRiskCheckpoint.from_database_row(tuple(row)) for row in rows)
        if any(load_candidate_risk_checkpoint(store, row.candidate_sha) != row for row in records):
            _invalid()
        return tuple(
            CandidateRiskRuntimeEvidence(
                row.phase, row.risk_level, row.affected_count, row.planned_at, row.completed_at
            )
            for row in records
        )
    except CandidateRiskExecutionError:
        raise
    except (CandidateRiskCheckpointError, StateError, sqlite3.Error):
        _invalid()


def _record_plan(
    store: StateStore, current: CandidateOrchestration, dependency: object, when: datetime
) -> CandidateRiskCheckpoint:
    from .candidate_dependency_checkpoint import CandidateDependencyCheckpoint

    if type(dependency) is not CandidateDependencyCheckpoint:
        _invalid()
    plan = CandidateRiskCheckpoint.plan(
        candidate_sha=current.candidate_sha,
        orchestration_sha256=current.record_sha256,
        dependency_sha256=dependency.record_sha256,
        target=current.target,
        repository_id=current.repository_id,
        baseline_sha=dependency.baseline_sha,
        stage_manifest_sha256=dependency.stage_manifest_sha256,
        planned_at=when,
    )
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if load_candidate_orchestration(store, current.candidate_sha) != current:
            _invalid()
        db.execute(
            "INSERT INTO candidate_risk_checkpoint VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            plan.database_values(),
        )
    persisted = load_candidate_risk_checkpoint(store, current.candidate_sha)
    if persisted != plan:
        _invalid()
    return plan


def _complete_atomically(
    store: StateStore,
    planned: CandidateRiskCheckpoint,
    completed: CandidateRiskCheckpoint,
    current: CandidateOrchestration,
    advanced: CandidateOrchestration,
) -> None:
    if completed.completed_at is None:
        _invalid()
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if load_candidate_risk_checkpoint(store, planned.candidate_sha) != planned:
            _invalid()
        first = db.execute(
            "UPDATE candidate_risk_checkpoint SET phase=?,impact_json=?,risk_json=?,"
            "risk_level=?,affected_count=?,completed_at=?,record_sha256=? "
            "WHERE candidate_sha=? AND phase='planned' AND record_sha256=?",
            (
                completed.phase,
                completed.impact_json,
                completed.risk_json,
                completed.risk_level,
                completed.affected_count,
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
        if first.rowcount != 1 or second.rowcount != 1:
            _invalid()


def _invalid() -> NoReturn:
    raise CandidateRiskExecutionError("Candidate risk evidence is invalid", transient=False)
