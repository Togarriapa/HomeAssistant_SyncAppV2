"""Crash-safe execution of exact candidate backup preparation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from .candidate_backup import (
    CandidateBackupError,
    CandidateBackupEvidence,
    create_candidate_backup,
    reconcile_candidate_backup,
)
from .candidate_backup_checkpoint import (
    CandidateBackupCheckpoint,
    CandidateBackupCheckpointError,
)
from .candidate_dependencies import CandidateDependencyAnalysis
from .candidate_dependency_execution import load_candidate_dependency_checkpoint
from .candidate_fetch_stage_execution import load_completed_candidate_stage
from .candidate_impact import CandidateImpactAnalysis
from .candidate_integrity import CandidateIntegrity
from .candidate_integrity_execution import (
    _integrity_from_checkpoint,
    load_candidate_integrity_checkpoint,
)
from .candidate_orchestration import (
    CandidateOrchestration,
    backup_blocked_candidate_orchestration,
    backup_prepared_candidate_orchestration,
    load_candidate_orchestration,
)
from .candidate_risk import CandidateRiskClassification
from .candidate_risk_execution import load_candidate_risk_checkpoint
from .candidate_semantic_execution import load_candidate_semantic_checkpoint
from .candidate_semantics import CandidateSemanticValidation
from .candidate_stage import CandidateStage
from .candidate_static_execution import load_candidate_static_checkpoint
from .candidate_validation import CandidateStaticValidation
from .core_version_evidence import CoreVersionEvidence, bind_core_version
from .prepared_deployment import PreparedDeployment, PreparedDeploymentError
from .runtime_inventory import RuntimeInventoryInput
from .state import StateError, StateStore


class CandidateBackupExecutionError(RuntimeError):
    """Candidate backup preparation could not proceed safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateBackupAuthority:
    semantic: CandidateSemanticValidation
    static: CandidateStaticValidation
    integrity: CandidateIntegrity
    stage: CandidateStage
    dependencies: CandidateDependencyAnalysis
    impact: CandidateImpactAnalysis
    risk: CandidateRiskClassification
    runtime: RuntimeInventoryInput
    version: CoreVersionEvidence
    fetch_stage_sha256: str
    integrity_sha256: str
    dependency_sha256: str
    risk_sha256: str
    static_sha256: str
    semantic_sha256: str


@dataclass(frozen=True, slots=True)
class CandidateBackupExecutionResult:
    checkpoint: CandidateBackupCheckpoint
    orchestration: CandidateOrchestration
    prepared: PreparedDeployment | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class CandidateBackupRuntimeEvidence:
    phase: str
    mutation_started: bool
    succeeded: bool | None
    planned_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


BackupCreator = Callable[..., CandidateBackupEvidence]
BackupReconciler = Callable[..., CandidateBackupEvidence]


def execute_candidate_backup_once(
    store: StateStore,
    orchestration: CandidateOrchestration,
    *,
    staging_root: Path,
    home_assistant_root: Path,
    token: str | None = None,
    creator: BackupCreator = create_candidate_backup,
    reconciler: BackupReconciler = reconcile_candidate_backup,
    now: datetime | None = None,
) -> CandidateBackupExecutionResult:
    """Perform at most one backup mutation or reconciliation action."""
    when = datetime.now(UTC) if now is None else now
    try:
        current = load_candidate_orchestration(store, orchestration.candidate_sha)
        if current is None or current != orchestration:
            _invalid()
        checkpoint = load_candidate_backup_checkpoint(store, current.candidate_sha)
        if checkpoint is not None and checkpoint.phase == "completed":
            prepared = store.prepared_deployment(checkpoint.deployment_id)
            if prepared is None or prepared.evidence != checkpoint.evidence():
                _invalid()
            return CandidateBackupExecutionResult(checkpoint, current, prepared, True)
        if checkpoint is not None and checkpoint.phase == "blocked":
            return CandidateBackupExecutionResult(checkpoint, current, None, True)

        authority = _load_authority(
            store,
            current,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
        )
        if checkpoint is None:
            if current.phase != "semantically_validated" or current.next_action != "prepare_backup":
                _invalid()
            checkpoint = _record_plan(store, current, authority, when)
        else:
            _verify_authority_binding(checkpoint, current, authority)

        if checkpoint.phase == "planned":
            started = checkpoint.start(started_at=when)
            _transition_checkpoint(store, checkpoint, started)
            try:
                evidence = creator(
                    authority.semantic,
                    authority.static,
                    authority.integrity,
                    authority.stage,
                    authority.dependencies,
                    authority.impact,
                    authority.risk,
                    authority.runtime,
                    authority.version,
                    backup_name=started.request_name,
                    token=token,
                )
            except CandidateBackupError:
                uncertain = started.mark_uncertain()
                _transition_checkpoint(store, started, uncertain)
                raise CandidateBackupExecutionError(
                    "Candidate backup outcome requires reconciliation", transient=True
                ) from None
            completed = started.complete(evidence, completed_at=when)
            return _finish_success(store, started, completed, current)

        if checkpoint.phase == "mutation_started":
            uncertain = checkpoint.mark_uncertain()
            _transition_checkpoint(store, checkpoint, uncertain)
            checkpoint = uncertain
        if checkpoint.phase != "uncertain" or checkpoint.started_at is None:
            _invalid()
        try:
            evidence = reconciler(
                authority.semantic,
                authority.static,
                authority.integrity,
                authority.stage,
                authority.dependencies,
                authority.impact,
                authority.risk,
                authority.runtime,
                authority.version,
                request_name=checkpoint.request_name,
                started_at=checkpoint.started_at,
                token=token,
            )
        except CandidateBackupError:
            blocked = checkpoint.block(completed_at=when)
            return _finish_block(store, checkpoint, blocked, current)
        completed = checkpoint.complete(evidence, completed_at=when)
        return _finish_success(store, checkpoint, completed, current)
    except CandidateBackupExecutionError:
        raise
    except (CandidateBackupCheckpointError, PreparedDeploymentError, StateError, sqlite3.Error):
        _invalid()


def load_candidate_backup_checkpoint(
    store: StateStore, candidate_sha: str
) -> CandidateBackupCheckpoint | None:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha,schema_version,orchestration_sha256,fetch_stage_sha256,"
            "integrity_sha256,dependency_sha256,risk_sha256,static_sha256,semantic_sha256,"
            "target,repository_id,baseline_sha,stage_manifest_sha256,runtime_sha256,risk_level,"
            "core_version,deployment_id,request_name,phase,backup_slug,planned_at,started_at,"
            "completed_at,record_sha256 FROM candidate_backup_checkpoint WHERE candidate_sha=?",
            (candidate_sha,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid()
        result = CandidateBackupCheckpoint.from_database_row(tuple(rows[0]))
        current = load_candidate_orchestration(store, candidate_sha)
        semantic = load_candidate_semantic_checkpoint(store, candidate_sha)
        if (
            current is None
            or semantic is None
            or semantic.phase != "completed"
            or result.target != current.target
            or result.repository_id != current.repository_id
            or result.fetch_stage_sha256 != semantic.fetch_stage_sha256
            or result.integrity_sha256 != semantic.integrity_sha256
            or result.dependency_sha256 != semantic.dependency_sha256
            or result.risk_sha256 != semantic.risk_sha256
            or result.static_sha256 != semantic.static_sha256
            or result.semantic_sha256 != semantic.record_sha256
            or (
                result.phase in {"planned", "mutation_started", "uncertain"}
                and current.phase != "semantically_validated"
            )
            or (result.phase == "completed" and current.phase != "completed")
            or (result.phase == "blocked" and current.phase != "blocked")
        ):
            _invalid()
        return result
    except CandidateBackupExecutionError:
        raise
    except (CandidateBackupCheckpointError, StateError, sqlite3.Error, TypeError, ValueError):
        _invalid()


def candidate_backup_runtime_evidence(
    store: StateStore,
) -> tuple[CandidateBackupRuntimeEvidence, ...]:
    """Return bounded, identity-free backup checkpoint evidence."""
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha FROM candidate_backup_checkpoint "
            "ORDER BY planned_at,candidate_sha LIMIT 65"
        ).fetchall()
        if len(rows) > 64:
            _invalid()
        result: list[CandidateBackupRuntimeEvidence] = []
        for row in rows:
            checkpoint = load_candidate_backup_checkpoint(store, str(row[0]))
            if checkpoint is None:
                _invalid()
            succeeded = (
                True
                if checkpoint.phase == "completed"
                else False
                if checkpoint.phase == "blocked"
                else None
            )
            result.append(
                CandidateBackupRuntimeEvidence(
                    checkpoint.phase,
                    checkpoint.started_at is not None,
                    succeeded,
                    checkpoint.planned_at,
                    checkpoint.started_at,
                    checkpoint.completed_at,
                )
            )
        return tuple(result)
    except CandidateBackupExecutionError:
        raise
    except (StateError, sqlite3.Error, TypeError, ValueError):
        _invalid()


def _load_authority(
    store: StateStore,
    current: CandidateOrchestration,
    *,
    staging_root: Path,
    home_assistant_root: Path,
) -> CandidateBackupAuthority:
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
    semantic_checkpoint = load_candidate_semantic_checkpoint(store, current.candidate_sha)
    if (
        integrity_checkpoint is None
        or dependency is None
        or risk_checkpoint is None
        or static_checkpoint is None
        or semantic_checkpoint is None
    ):
        _invalid()
    integrity = _integrity_from_checkpoint(integrity_checkpoint, integrity_checkpoint.changes())
    dependencies, runtime = dependency.dependencies(), dependency.runtime()
    risk = risk_checkpoint.risk(dependencies, runtime)
    return CandidateBackupAuthority(
        semantic_checkpoint.semantic(),
        static_checkpoint.validation(),
        integrity,
        stage,
        dependencies,
        risk_checkpoint.impact(dependencies, runtime),
        risk,
        runtime,
        bind_core_version(runtime),
        fetch.record_sha256,
        integrity_checkpoint.record_sha256,
        dependency.record_sha256,
        risk_checkpoint.record_sha256,
        static_checkpoint.record_sha256,
        semantic_checkpoint.record_sha256,
    )


def load_candidate_backup_authority(
    store: StateStore,
    current: CandidateOrchestration,
    *,
    staging_root: Path,
    home_assistant_root: Path,
) -> CandidateBackupAuthority:
    """Reconstruct the exact validated authority used by backup and Apply gates."""
    return _load_authority(
        store,
        current,
        staging_root=staging_root,
        home_assistant_root=home_assistant_root,
    )


def _record_plan(
    store: StateStore,
    current: CandidateOrchestration,
    authority: CandidateBackupAuthority,
    when: datetime,
) -> CandidateBackupCheckpoint:
    semantic = authority.semantic
    plan = CandidateBackupCheckpoint.plan(
        candidate_sha=current.candidate_sha,
        orchestration_sha256=current.record_sha256,
        fetch_stage_sha256=authority.fetch_stage_sha256,
        integrity_sha256=authority.integrity_sha256,
        dependency_sha256=authority.dependency_sha256,
        risk_sha256=authority.risk_sha256,
        static_sha256=authority.static_sha256,
        semantic_sha256=authority.semantic_sha256,
        target=current.target,
        repository_id=current.repository_id,
        baseline_sha=semantic.baseline_sha,
        stage_manifest_sha256=semantic.stage_manifest_sha256,
        runtime_sha256=semantic.runtime_sha256,
        risk_level=semantic.risk_level,
        core_version=semantic.core_version,
        planned_at=when,
    )
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if load_candidate_orchestration(store, current.candidate_sha) != current:
            _invalid()
        db.execute(
            "INSERT INTO candidate_backup_checkpoint VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            plan.database_values(),
        )
    if load_candidate_backup_checkpoint(store, current.candidate_sha) != plan:
        _invalid()
    return plan


def _verify_authority_binding(
    checkpoint: CandidateBackupCheckpoint,
    current: CandidateOrchestration,
    authority: CandidateBackupAuthority,
) -> None:
    semantic = authority.semantic
    if (
        checkpoint.orchestration_sha256 != current.record_sha256
        or checkpoint.fetch_stage_sha256 != authority.fetch_stage_sha256
        or checkpoint.integrity_sha256 != authority.integrity_sha256
        or checkpoint.dependency_sha256 != authority.dependency_sha256
        or checkpoint.risk_sha256 != authority.risk_sha256
        or checkpoint.static_sha256 != authority.static_sha256
        or checkpoint.semantic_sha256 != authority.semantic_sha256
        or checkpoint.baseline_sha != semantic.baseline_sha
        or checkpoint.stage_manifest_sha256 != semantic.stage_manifest_sha256
        or checkpoint.runtime_sha256 != semantic.runtime_sha256
        or checkpoint.risk_level != semantic.risk_level
        or checkpoint.core_version != semantic.core_version
    ):
        _invalid()


def _transition_checkpoint(
    store: StateStore,
    previous: CandidateBackupCheckpoint,
    replacement: CandidateBackupCheckpoint,
) -> None:
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            "UPDATE candidate_backup_checkpoint SET phase=?,backup_slug=?,started_at=?,"
            "completed_at=?,record_sha256=? WHERE candidate_sha=? AND record_sha256=?",
            (
                replacement.phase,
                replacement.backup_slug,
                _optional_time(replacement.started_at),
                _optional_time(replacement.completed_at),
                replacement.record_sha256,
                previous.candidate_sha,
                previous.record_sha256,
            ),
        )
        if changed.rowcount != 1:
            _invalid()
    if load_candidate_backup_checkpoint(store, previous.candidate_sha) != replacement:
        _invalid()


def _finish_success(
    store: StateStore,
    previous: CandidateBackupCheckpoint,
    completed: CandidateBackupCheckpoint,
    current: CandidateOrchestration,
) -> CandidateBackupExecutionResult:
    if completed.completed_at is None:
        _invalid()
    prepared = PreparedDeployment(
        completed.deployment_id, completed.evidence(), completed.completed_at
    )
    prepared_values = prepared.database_values()
    advanced = backup_prepared_candidate_orchestration(current, updated_at=completed.completed_at)
    _finish_atomically(store, previous, completed, current, advanced, prepared_values)
    loaded = store.prepared_deployment(completed.deployment_id)
    if loaded != prepared:
        _invalid()
    return CandidateBackupExecutionResult(completed, advanced, loaded, False)


def _finish_block(
    store: StateStore,
    previous: CandidateBackupCheckpoint,
    blocked: CandidateBackupCheckpoint,
    current: CandidateOrchestration,
) -> CandidateBackupExecutionResult:
    if blocked.completed_at is None:
        _invalid()
    advanced = backup_blocked_candidate_orchestration(current, updated_at=blocked.completed_at)
    _finish_atomically(store, previous, blocked, current, advanced, None)
    return CandidateBackupExecutionResult(blocked, advanced, None, False)


def _finish_atomically(
    store: StateStore,
    previous: CandidateBackupCheckpoint,
    finished: CandidateBackupCheckpoint,
    current: CandidateOrchestration,
    advanced: CandidateOrchestration,
    prepared_values: tuple[object, ...] | None,
) -> None:
    if finished.completed_at is None:
        _invalid()
    status = "succeeded" if finished.phase == "completed" else "blocked"
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        first = db.execute(
            "UPDATE candidate_backup_checkpoint SET phase=?,backup_slug=?,started_at=?,"
            "completed_at=?,record_sha256=? WHERE candidate_sha=? AND record_sha256=?",
            (
                finished.phase,
                finished.backup_slug,
                _optional_time(finished.started_at),
                _optional_time(finished.completed_at),
                finished.record_sha256,
                previous.candidate_sha,
                previous.record_sha256,
            ),
        )
        if prepared_values is not None:
            db.execute(
                "INSERT INTO prepared_deployment VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                prepared_values,
            )
            completed_at = finished.completed_at.astimezone(UTC).isoformat()
            db.execute(
                "INSERT INTO work (work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at) VALUES ('candidate_apply',?,'pending',0,?,?,?)",
                (finished.deployment_id, completed_at, completed_at, completed_at),
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
        third = db.execute(
            "UPDATE work SET status=?,updated_at=?,next_attempt_at=NULL WHERE "
            "work_kind='candidate' "
            "AND work_key=? AND status='running'",
            (
                status,
                finished.completed_at.astimezone(UTC).isoformat(),
                current.candidate_sha,
            ),
        )
        if first.rowcount != 1 or second.rowcount != 1 or third.rowcount != 1:
            _invalid()
    if (
        load_candidate_backup_checkpoint(store, current.candidate_sha) != finished
        or load_candidate_orchestration(store, current.candidate_sha) != advanced
    ):
        _invalid()


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _invalid() -> NoReturn:
    raise CandidateBackupExecutionError("Candidate backup evidence is invalid", transient=False)
