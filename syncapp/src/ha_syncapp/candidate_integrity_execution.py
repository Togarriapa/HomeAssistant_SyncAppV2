"""Crash-safe execution of exact candidate change and integrity analysis."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from .candidate_changes import CandidateChangeError, CandidateChanges, detect_candidate_changes
from .candidate_detection import (
    CandidateDetectionError,
    CandidateObservation,
    observe_trusted_candidate,
)
from .candidate_fetch import CandidateFetch, CandidateFetchError, fetch_trusted_candidate
from .candidate_fetch_stage_execution import (
    CandidateFetchStageExecutionError,
    _remove_fetch_workspace,
    load_completed_candidate_stage,
)
from .candidate_integrity import (
    CandidateIntegrity,
    CandidateIntegrityError,
    validate_candidate_integrity,
    verify_candidate_integrity,
)
from .candidate_integrity_checkpoint import (
    CandidateIntegrityCheckpoint,
    CandidateIntegrityCheckpointError,
)
from .candidate_orchestration import (
    CandidateOrchestration,
    CandidateOrchestrationError,
    integrity_verified_candidate_orchestration,
    load_candidate_orchestration,
)
from .candidate_stage import CandidateStage
from .github_repo import RepositoryVerificationError
from .state import StateError, StateStore

_MAX_DISCOVERABLE = 64


class CandidateIntegrityExecutionError(RuntimeError):
    """Candidate analysis could not proceed safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateIntegrityExecutionResult:
    checkpoint: CandidateIntegrityCheckpoint
    orchestration: CandidateOrchestration
    integrity: CandidateIntegrity
    changes: CandidateChanges
    replayed: bool


@dataclass(frozen=True, slots=True)
class CandidateIntegrityRuntimeEvidence:
    """Content-free candidate analysis metadata for runtime diagnostics."""

    phase: str
    changed_count: int | None
    planned_at: datetime
    completed_at: datetime | None


Observer = Callable[..., CandidateObservation]
Fetcher = Callable[..., CandidateFetch]
ChangeDetector = Callable[[CandidateFetch, CandidateStage, str], CandidateChanges]
IntegrityValidator = Callable[
    [CandidateFetch, CandidateStage, CandidateChanges], CandidateIntegrity
]


def execute_candidate_integrity_once(
    store: StateStore,
    orchestration: CandidateOrchestration,
    *,
    token: str | None,
    workspace_root: Path,
    staging_root: Path,
    home_assistant_root: Path,
    observer: Observer = observe_trusted_candidate,
    fetcher: Fetcher = fetch_trusted_candidate,
    change_detector: ChangeDetector = detect_candidate_changes,
    integrity_validator: IntegrityValidator = validate_candidate_integrity,
    now: datetime | None = None,
) -> CandidateIntegrityExecutionResult:
    """Execute or replay one evidence-bound, read-only candidate analysis."""
    fetched: CandidateFetch | None = None
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
        checkpoint = load_candidate_integrity_checkpoint(store, current.candidate_sha)
        if checkpoint is not None and checkpoint.phase == "completed":
            if (
                current.phase != "integrity_verified"
                or current.next_action != "analyze_dependencies"
            ):
                _invalid()
            changes = checkpoint.changes()
            integrity = _integrity_from_checkpoint(checkpoint, changes)
            verify_candidate_integrity(integrity, stage, changes)
            return CandidateIntegrityExecutionResult(
                checkpoint, current, integrity, changes, True
            )
        if current.phase != "staged" or current.next_action != "analyze":
            _invalid()
        if checkpoint is None:
            checkpoint = _record_plan(
                store, current, fetch_checkpoint.record_sha256, stage, current_time
            )
        elif (
            checkpoint.orchestration_sha256 != current.record_sha256
            or checkpoint.fetch_stage_sha256 != fetch_checkpoint.record_sha256
            or checkpoint.stage_manifest_sha256 != stage.manifest_sha256
        ):
            _invalid()
        if token is None:
            raise CandidateIntegrityExecutionError(
                "Candidate analysis credentials are unavailable", transient=True
            )
        observation = observer(current.target, token, expected_id=current.repository_id)
        if (
            observation.target != current.target
            or observation.repository_id != current.repository_id
            or observation.branch != "candidate"
            or observation.commit_sha != current.candidate_sha
        ):
            _invalid()
        fetched = fetcher(
            observation,
            current.candidate_sha,
            token,
            workspace_root,
            home_assistant_root,
        )
        changes = change_detector(fetched, stage, token)
        integrity = integrity_validator(fetched, stage, changes)
        verify_candidate_integrity(integrity, stage, changes)
        completed = checkpoint.complete(changes, completed_at=current_time)
        if _integrity_from_checkpoint(completed, changes) != integrity:
            _invalid()
        advanced = integrity_verified_candidate_orchestration(current, updated_at=current_time)
        _complete_atomically(store, checkpoint, completed, current, advanced)
        return CandidateIntegrityExecutionResult(
            completed, advanced, integrity, changes, False
        )
    except CandidateIntegrityExecutionError:
        raise
    except (CandidateFetchError, RepositoryVerificationError, CandidateChangeError) as error:
        transient = _transient_external_failure(error)
        raise CandidateIntegrityExecutionError(
            "Candidate analysis is temporarily unavailable"
            if transient
            else "Candidate analysis evidence is invalid",
            transient=transient,
        ) from None
    except (
        CandidateDetectionError,
        CandidateFetchStageExecutionError,
        CandidateIntegrityError,
        CandidateIntegrityCheckpointError,
        CandidateOrchestrationError,
        StateError,
        sqlite3.Error,
        OSError,
        TypeError,
        ValueError,
    ):
        raise CandidateIntegrityExecutionError(
            "Candidate analysis evidence is invalid", transient=False
        ) from None
    finally:
        if fetched is not None:
            _remove_fetch_workspace(fetched.root, workspace_root)


def load_candidate_integrity_checkpoint(
    store: StateStore,
    candidate_sha: str,
) -> CandidateIntegrityCheckpoint | None:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha, schema_version, orchestration_sha256, "
            "fetch_stage_sha256, target, repository_id, stage_manifest_sha256, phase, "
            "baseline_sha, changes_json, changed_count, planned_at, completed_at, "
            "record_sha256 FROM candidate_integrity_checkpoint WHERE candidate_sha = ?",
            (candidate_sha,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid()
        result = CandidateIntegrityCheckpoint.from_database_row(rows[0])
        current = load_candidate_orchestration(store, result.candidate_sha)
        if (
            current is None
            or current.target != result.target
            or current.repository_id != result.repository_id
            or (result.phase == "planned" and current.record_sha256 != result.orchestration_sha256)
            or (result.phase == "completed" and current.phase != "integrity_verified")
        ):
            _invalid()
        return result
    except CandidateIntegrityExecutionError:
        raise
    except (
        CandidateIntegrityCheckpointError,
        CandidateOrchestrationError,
        StateError,
        sqlite3.Error,
    ):
        _invalid()


def candidate_integrity_runtime_evidence(
    store: StateStore,
) -> tuple[CandidateIntegrityRuntimeEvidence, ...]:
    """Read bounded candidate-analysis metadata without candidate identity or paths."""
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha, schema_version, orchestration_sha256, "
            "fetch_stage_sha256, target, repository_id, stage_manifest_sha256, phase, "
            "baseline_sha, changes_json, changed_count, planned_at, completed_at, "
            "record_sha256 FROM candidate_integrity_checkpoint "
            "ORDER BY planned_at, candidate_sha LIMIT ?",
            (_MAX_DISCOVERABLE + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE:
            _invalid()
        checkpoints = tuple(
            CandidateIntegrityCheckpoint.from_database_row(tuple(row)) for row in rows
        )
        if any(
            load_candidate_integrity_checkpoint(store, checkpoint.candidate_sha) != checkpoint
            for checkpoint in checkpoints
        ):
            _invalid()
        return tuple(
            CandidateIntegrityRuntimeEvidence(
                phase=checkpoint.phase,
                changed_count=checkpoint.changed_count,
                planned_at=checkpoint.planned_at,
                completed_at=checkpoint.completed_at,
            )
            for checkpoint in checkpoints
        )
    except CandidateIntegrityExecutionError:
        raise
    except (CandidateIntegrityCheckpointError, StateError, sqlite3.Error):
        _invalid()


def _record_plan(
    store: StateStore,
    orchestration: CandidateOrchestration,
    fetch_stage_sha256: str,
    stage: CandidateStage,
    when: datetime,
) -> CandidateIntegrityCheckpoint:
    plan = CandidateIntegrityCheckpoint.plan(
        candidate_sha=orchestration.candidate_sha,
        orchestration_sha256=orchestration.record_sha256,
        fetch_stage_sha256=fetch_stage_sha256,
        target=orchestration.target,
        repository_id=orchestration.repository_id,
        stage_manifest_sha256=stage.manifest_sha256,
        planned_at=when,
    )
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if load_candidate_orchestration(store, orchestration.candidate_sha) != orchestration:
            _invalid()
        db.execute(
            "INSERT INTO candidate_integrity_checkpoint "
            "(candidate_sha, schema_version, orchestration_sha256, fetch_stage_sha256, "
            "target, repository_id, stage_manifest_sha256, phase, baseline_sha, changes_json, "
            "changed_count, planned_at, completed_at, record_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            plan.database_values(),
        )
    persisted = load_candidate_integrity_checkpoint(store, orchestration.candidate_sha)
    if persisted != plan:
        _invalid()
    return plan


def _complete_atomically(
    store: StateStore,
    planned: CandidateIntegrityCheckpoint,
    completed: CandidateIntegrityCheckpoint,
    current: CandidateOrchestration,
    advanced: CandidateOrchestration,
) -> None:
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            load_candidate_integrity_checkpoint(store, planned.candidate_sha) != planned
            or load_candidate_orchestration(store, current.candidate_sha) != current
        ):
            _invalid()
        checkpoint_result = db.execute(
            "UPDATE candidate_integrity_checkpoint SET phase = ?, baseline_sha = ?, "
            "changes_json = ?, changed_count = ?, completed_at = ?, record_sha256 = ? "
            "WHERE candidate_sha = ? AND phase = 'planned' AND record_sha256 = ?",
            (
                completed.phase,
                completed.baseline_sha,
                completed.changes_json,
                completed.changed_count,
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


def _integrity_from_checkpoint(
    checkpoint: CandidateIntegrityCheckpoint,
    changes: CandidateChanges,
) -> CandidateIntegrity:
    if checkpoint.baseline_sha is None:
        _invalid()
    return CandidateIntegrity(
        target=checkpoint.target,
        repository_id=checkpoint.repository_id,
        baseline_sha=checkpoint.baseline_sha,
        candidate_sha=checkpoint.candidate_sha,
        stage_manifest_sha256=checkpoint.stage_manifest_sha256,
        changed_paths=tuple(change.path for change in changes.changes),
    )


def _transient_external_failure(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        message = str(current)
        if isinstance(current, RepositoryVerificationError) and (
            "transport failed" in message
            or any(f"HTTP {code}" in message for code in (408, 429, 500, 502, 503, 504))
        ):
            return True
        if isinstance(current, CandidateFetchError) and (
            "command failed" in message
            or "fetch failed" in message
            or "could not execute" in message
        ):
            return True
        current = current.__cause__
    return False


def _invalid() -> NoReturn:
    raise CandidateIntegrityExecutionError(
        "Candidate analysis evidence is invalid", transient=False
    )
