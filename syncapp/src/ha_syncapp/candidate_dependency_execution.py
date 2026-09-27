"""Crash-safe execution of exact candidate dependency analysis."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from .candidate_dependencies import (
    CandidateDependencyAnalysis,
    CandidateDependencyError,
    analyze_candidate_dependencies,
    verify_candidate_dependency_analysis,
)
from .candidate_dependency_checkpoint import (
    CandidateDependencyCheckpoint,
    CandidateDependencyCheckpointError,
)
from .candidate_fetch_stage_execution import (
    CandidateFetchStageExecutionError,
    load_completed_candidate_stage,
)
from .candidate_integrity import (
    CandidateIntegrity,
    CandidateIntegrityError,
    verify_candidate_integrity,
)
from .candidate_integrity_checkpoint import CandidateIntegrityCheckpointError
from .candidate_integrity_execution import (
    CandidateIntegrityExecutionError,
    load_candidate_integrity_checkpoint,
)
from .candidate_orchestration import (
    CandidateOrchestration,
    CandidateOrchestrationError,
    dependencies_analyzed_candidate_orchestration,
    load_candidate_orchestration,
)
from .candidate_stage import CandidateStage
from .core_runtime_bundle import CoreRuntimeBundleError, collect_core_runtime_bundle
from .runtime_inventory import RuntimeInventoryInput
from .state import StateError, StateStore

_MAX_DISCOVERABLE = 64


class CandidateDependencyExecutionError(RuntimeError):
    """Candidate dependency analysis could not proceed safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateDependencyExecutionResult:
    checkpoint: CandidateDependencyCheckpoint
    orchestration: CandidateOrchestration
    dependencies: CandidateDependencyAnalysis
    runtime: RuntimeInventoryInput
    replayed: bool


@dataclass(frozen=True, slots=True)
class CandidateDependencyRuntimeEvidence:
    """Content-free dependency metadata for runtime diagnostics."""

    phase: str
    reference_count: int | None
    planned_at: datetime
    completed_at: datetime | None


RuntimeCollector = Callable[..., RuntimeInventoryInput]
Analyzer = Callable[
    [CandidateIntegrity, CandidateStage, RuntimeInventoryInput], CandidateDependencyAnalysis
]


def execute_candidate_dependency_once(
    store: StateStore,
    orchestration: CandidateOrchestration,
    *,
    core_token: str | None,
    staging_root: Path,
    home_assistant_root: Path,
    runtime_collector: RuntimeCollector = collect_core_runtime_bundle,
    analyzer: Analyzer = analyze_candidate_dependencies,
    now: datetime | None = None,
) -> CandidateDependencyExecutionResult:
    """Execute or replay one integrity-bound, read-only dependency analysis."""
    current_time = datetime.now(UTC) if now is None else now
    try:
        current = load_candidate_orchestration(store, orchestration.candidate_sha)
        if current is None or current != orchestration:
            _invalid()
        fetch_checkpoint, stage = load_completed_candidate_stage(
            store,
            current,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
        )
        integrity_checkpoint = load_candidate_integrity_checkpoint(store, current.candidate_sha)
        if integrity_checkpoint is None or integrity_checkpoint.phase != "completed":
            _invalid()
        changes = integrity_checkpoint.changes()
        integrity = CandidateIntegrity(
            target=integrity_checkpoint.target,
            repository_id=integrity_checkpoint.repository_id,
            baseline_sha=changes.baseline_sha,
            candidate_sha=integrity_checkpoint.candidate_sha,
            stage_manifest_sha256=integrity_checkpoint.stage_manifest_sha256,
            changed_paths=tuple(change.path for change in changes.changes),
        )
        verify_candidate_integrity(integrity, stage, changes)

        checkpoint = load_candidate_dependency_checkpoint(store, current.candidate_sha)
        if checkpoint is not None and checkpoint.phase == "completed":
            if current.phase != "dependencies_analyzed" or current.next_action != "classify_risk":
                _invalid()
            runtime = checkpoint.runtime()
            dependencies = checkpoint.dependencies()
            verify_candidate_dependency_analysis(dependencies, runtime)
            return CandidateDependencyExecutionResult(
                checkpoint, current, dependencies, runtime, True
            )
        if current.phase != "integrity_verified" or current.next_action != "analyze_dependencies":
            _invalid()
        if checkpoint is None:
            checkpoint = _record_plan(
                store,
                current,
                fetch_checkpoint.record_sha256,
                integrity_checkpoint.record_sha256,
                integrity,
                current_time,
            )
        elif (
            checkpoint.orchestration_sha256 != current.record_sha256
            or checkpoint.fetch_stage_sha256 != fetch_checkpoint.record_sha256
            or checkpoint.integrity_sha256 != integrity_checkpoint.record_sha256
            or checkpoint.baseline_sha != integrity.baseline_sha
            or checkpoint.stage_manifest_sha256 != stage.manifest_sha256
        ):
            _invalid()
        if core_token is None:
            raise CandidateDependencyExecutionError(
                "Candidate dependency credentials are unavailable", transient=True
            )
        runtime = runtime_collector(token=core_token)
        dependencies = analyzer(integrity, stage, runtime)
        verify_candidate_dependency_analysis(dependencies, runtime)
        completed = checkpoint.complete(dependencies, runtime, completed_at=current_time)
        advanced = dependencies_analyzed_candidate_orchestration(current, updated_at=current_time)
        _complete_atomically(store, checkpoint, completed, current, advanced)
        return CandidateDependencyExecutionResult(completed, advanced, dependencies, runtime, False)
    except CandidateDependencyExecutionError:
        raise
    except CoreRuntimeBundleError:
        raise CandidateDependencyExecutionError(
            "Candidate dependency runtime is temporarily unavailable", transient=True
        ) from None
    except (
        CandidateDependencyError,
        CandidateDependencyCheckpointError,
        CandidateFetchStageExecutionError,
        CandidateIntegrityCheckpointError,
        CandidateIntegrityExecutionError,
        CandidateIntegrityError,
        CandidateOrchestrationError,
        StateError,
        sqlite3.Error,
        OSError,
        TypeError,
        ValueError,
    ):
        _invalid()


def load_candidate_dependency_checkpoint(
    store: StateStore,
    candidate_sha: str,
) -> CandidateDependencyCheckpoint | None:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha, schema_version, orchestration_sha256, "
            "fetch_stage_sha256, integrity_sha256, target, repository_id, baseline_sha, "
            "stage_manifest_sha256, phase, runtime_json, dependencies_json, "
            "reference_count, planned_at, completed_at, record_sha256 "
            "FROM candidate_dependency_checkpoint WHERE candidate_sha = ?",
            (candidate_sha,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid()
        result = CandidateDependencyCheckpoint.from_database_row(tuple(rows[0]))
        current = load_candidate_orchestration(store, result.candidate_sha)
        if (
            current is None
            or current.target != result.target
            or current.repository_id != result.repository_id
            or (result.phase == "planned" and current.record_sha256 != result.orchestration_sha256)
            or (
                result.phase == "completed"
                and current.phase
                not in {
                    "dependencies_analyzed",
                    "risk_classified",
                    "static_validated",
                    "semantically_validated",
                    "completed",
                    "blocked",
                }
            )
        ):
            _invalid()
        return result
    except CandidateDependencyExecutionError:
        raise
    except (
        CandidateDependencyCheckpointError,
        CandidateOrchestrationError,
        StateError,
        sqlite3.Error,
    ):
        _invalid()


def candidate_dependency_runtime_evidence(
    store: StateStore,
) -> tuple[CandidateDependencyRuntimeEvidence, ...]:
    """Read bounded dependency checkpoint metadata without identities or contents."""
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha, schema_version, orchestration_sha256, "
            "fetch_stage_sha256, integrity_sha256, target, repository_id, baseline_sha, "
            "stage_manifest_sha256, phase, runtime_json, dependencies_json, "
            "reference_count, planned_at, completed_at, record_sha256 "
            "FROM candidate_dependency_checkpoint "
            "ORDER BY planned_at, candidate_sha LIMIT ?",
            (_MAX_DISCOVERABLE + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE:
            _invalid()
        checkpoints = tuple(
            CandidateDependencyCheckpoint.from_database_row(tuple(row)) for row in rows
        )
        if any(
            load_candidate_dependency_checkpoint(store, item.candidate_sha) != item
            for item in checkpoints
        ):
            _invalid()
        return tuple(
            CandidateDependencyRuntimeEvidence(
                item.phase,
                item.reference_count,
                item.planned_at,
                item.completed_at,
            )
            for item in checkpoints
        )
    except CandidateDependencyExecutionError:
        raise
    except (CandidateDependencyCheckpointError, StateError, sqlite3.Error):
        _invalid()


def _record_plan(
    store: StateStore,
    orchestration: CandidateOrchestration,
    fetch_stage_sha256: str,
    integrity_sha256: str,
    integrity: CandidateIntegrity,
    when: datetime,
) -> CandidateDependencyCheckpoint:
    plan = CandidateDependencyCheckpoint.plan(
        candidate_sha=orchestration.candidate_sha,
        orchestration_sha256=orchestration.record_sha256,
        fetch_stage_sha256=fetch_stage_sha256,
        integrity_sha256=integrity_sha256,
        target=orchestration.target,
        repository_id=orchestration.repository_id,
        baseline_sha=integrity.baseline_sha,
        stage_manifest_sha256=integrity.stage_manifest_sha256,
        planned_at=when,
    )
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if load_candidate_orchestration(store, orchestration.candidate_sha) != orchestration:
            _invalid()
        db.execute(
            "INSERT INTO candidate_dependency_checkpoint "
            "(candidate_sha, schema_version, orchestration_sha256, fetch_stage_sha256, "
            "integrity_sha256, target, repository_id, baseline_sha, stage_manifest_sha256, "
            "phase, runtime_json, dependencies_json, reference_count, planned_at, "
            "completed_at, record_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            plan.database_values(),
        )
    persisted = load_candidate_dependency_checkpoint(store, orchestration.candidate_sha)
    if persisted != plan:
        _invalid()
    return plan


def _complete_atomically(
    store: StateStore,
    planned: CandidateDependencyCheckpoint,
    completed: CandidateDependencyCheckpoint,
    current: CandidateOrchestration,
    advanced: CandidateOrchestration,
) -> None:
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            load_candidate_dependency_checkpoint(store, planned.candidate_sha) != planned
            or load_candidate_orchestration(store, current.candidate_sha) != current
        ):
            _invalid()
        checkpoint_result = db.execute(
            "UPDATE candidate_dependency_checkpoint SET phase = ?, runtime_json = ?, "
            "dependencies_json = ?, reference_count = ?, completed_at = ?, record_sha256 = ? "
            "WHERE candidate_sha = ? AND phase = 'planned' AND record_sha256 = ?",
            (
                completed.phase,
                completed.runtime_json,
                completed.dependencies_json,
                completed.reference_count,
                completed.completed_at.astimezone(UTC).isoformat()
                if completed.completed_at is not None
                else None,
                completed.record_sha256,
                planned.candidate_sha,
                planned.record_sha256,
            ),
        )
        orchestration_result = db.execute(
            "UPDATE candidate_orchestration SET phase = ?, next_action = ?, updated_at = ?, "
            "record_sha256 = ? WHERE candidate_sha = ? AND record_sha256 = ?",
            (
                advanced.phase,
                advanced.next_action,
                advanced.updated_at.astimezone(UTC).isoformat(),
                advanced.record_sha256,
                current.candidate_sha,
                current.record_sha256,
            ),
        )
        if checkpoint_result.rowcount != 1 or orchestration_result.rowcount != 1:
            _invalid()


def _invalid() -> NoReturn:
    raise CandidateDependencyExecutionError(
        "Candidate dependency evidence is invalid", transient=False
    )
