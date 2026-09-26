"""Crash-safe execution and checkpointing of one candidate Fetch/Stage action."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import UUID, uuid4

from .candidate_detection import (
    CandidateDetectionError,
    CandidateObservation,
    observe_trusted_candidate,
)
from .candidate_fetch import CandidateFetch, CandidateFetchError, fetch_trusted_candidate
from .candidate_orchestration import (
    CandidateOrchestration,
    CandidateOrchestrationError,
    load_candidate_orchestration,
    staged_candidate_orchestration,
)
from .candidate_stage import (
    CandidateStage,
    CandidateStageError,
    load_candidate_stage,
    stage_fetched_candidate,
    verify_candidate_stage,
)
from .github_repo import RepositoryVerificationError
from .state import StateError, StateStore

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SCHEMA_VERSION = 1
_FINAL_PREFIX = ".syncapp-candidate-stage-"
_TEMP_PREFIX = ".git-workspace-candidate-stage-"
_MAX_DISCOVERABLE = 64


class CandidateFetchStageExecutionError(RuntimeError):
    """Fetch/Stage could not proceed without weakening its authority boundary."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateFetchStageCheckpoint:
    candidate_sha: str
    schema_version: int
    orchestration_sha256: str
    target: str
    repository_id: int
    workspace_id: str
    phase: str
    manifest_sha256: str | None
    entry_count: int | None
    total_bytes: int | None
    planned_at: datetime
    completed_at: datetime | None
    record_sha256: str

    @classmethod
    def plan(
        cls,
        orchestration: CandidateOrchestration,
        *,
        workspace_id: str,
        planned_at: datetime,
    ) -> CandidateFetchStageCheckpoint:
        when = _timestamp(planned_at)
        values: tuple[object, ...] = (
            orchestration.candidate_sha,
            _SCHEMA_VERSION,
            orchestration.record_sha256,
            orchestration.target,
            orchestration.repository_id,
            workspace_id,
            "planned",
            None,
            None,
            None,
            when.isoformat(),
            None,
        )
        result = cls(
            orchestration.candidate_sha,
            _SCHEMA_VERSION,
            orchestration.record_sha256,
            orchestration.target,
            orchestration.repository_id,
            workspace_id,
            "planned",
            None,
            None,
            None,
            when,
            None,
            _digest(values),
        )
        result.validate()
        return result

    def complete(
        self,
        stage: CandidateStage,
        *,
        completed_at: datetime,
    ) -> CandidateFetchStageCheckpoint:
        when = _timestamp(completed_at)
        result = CandidateFetchStageCheckpoint(
            candidate_sha=self.candidate_sha,
            schema_version=self.schema_version,
            orchestration_sha256=self.orchestration_sha256,
            target=self.target,
            repository_id=self.repository_id,
            workspace_id=self.workspace_id,
            phase="completed",
            manifest_sha256=stage.manifest_sha256,
            entry_count=len(stage.entries),
            total_bytes=sum(entry.size for entry in stage.entries),
            planned_at=self.planned_at,
            completed_at=when,
            record_sha256="0" * 64,
        )
        return replace(result, record_sha256=_digest(result.values_without_digest()))

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CandidateFetchStageCheckpoint:
        if len(row) != 13 or type(row[4]) is not int:
            _invalid()
        try:
            planned = datetime.fromisoformat(_text(row[10]))
            completed = None if row[11] is None else datetime.fromisoformat(_text(row[11]))
        except ValueError:
            _invalid()
        result = cls(
            _text(row[0]),
            _integer(row[1]),
            _text(row[2]),
            _text(row[3]),
            _integer(row[4]),
            _text(row[5]),
            _text(row[6]),
            None if row[7] is None else _text(row[7]),
            None if row[8] is None else _integer(row[8]),
            None if row[9] is None else _integer(row[9]),
            planned,
            completed,
            _text(row[12]),
        )
        result.validate()
        if result.database_values() != row:
            _invalid()
        return result

    def values_without_digest(self) -> tuple[object, ...]:
        return (
            self.candidate_sha,
            self.schema_version,
            self.orchestration_sha256,
            self.target,
            self.repository_id,
            self.workspace_id,
            self.phase,
            self.manifest_sha256,
            self.entry_count,
            self.total_bytes,
            self.planned_at.astimezone(UTC).isoformat(),
            None if self.completed_at is None else self.completed_at.astimezone(UTC).isoformat(),
        )

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values = self.values_without_digest()
        return (*values, _digest(values))

    def validate(self) -> None:
        try:
            canonical_workspace = str(UUID(self.workspace_id)) == self.workspace_id
        except ValueError:
            canonical_workspace = False
        completed = self.phase == "completed"
        if (
            _COMMIT.fullmatch(self.candidate_sha) is None
            or self.schema_version != _SCHEMA_VERSION
            or _HASH.fullmatch(self.orchestration_sha256) is None
            or _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or not canonical_workspace
            or self.phase not in {"planned", "completed"}
            or completed != (self.manifest_sha256 is not None)
            or completed != (self.entry_count is not None)
            or completed != (self.total_bytes is not None)
            or completed != (self.completed_at is not None)
            or (self.manifest_sha256 is not None and _HASH.fullmatch(self.manifest_sha256) is None)
            or (self.entry_count is not None and self.entry_count < 0)
            or (self.total_bytes is not None and self.total_bytes < 0)
            or self.planned_at.tzinfo is None
            or self.planned_at.utcoffset() is None
            or (self.completed_at is not None and self.completed_at < self.planned_at)
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid()


@dataclass(frozen=True, slots=True)
class CandidateFetchStageResult:
    checkpoint: CandidateFetchStageCheckpoint
    orchestration: CandidateOrchestration
    replayed: bool


@dataclass(frozen=True, slots=True)
class CandidateFetchStageRuntimeEvidence:
    """Content-free checkpoint state for runtime and AI diagnostics."""

    phase: str
    entry_count: int | None
    total_bytes: int | None
    planned_at: datetime
    completed_at: datetime | None


Observer = Callable[..., CandidateObservation]
Fetcher = Callable[..., CandidateFetch]
Stager = Callable[..., CandidateStage]


def execute_candidate_fetch_stage_once(
    store: StateStore,
    orchestration: CandidateOrchestration,
    *,
    token: str | None,
    workspace_root: Path,
    staging_root: Path,
    home_assistant_root: Path,
    observer: Observer = observe_trusted_candidate,
    fetcher: Fetcher = fetch_trusted_candidate,
    stager: Stager = stage_fetched_candidate,
    now: datetime | None = None,
) -> CandidateFetchStageResult:
    """Execute or replay exactly one evidence-bound Fetch/Stage action."""
    fetched: CandidateFetch | None = None
    current_time = datetime.now(UTC) if now is None else now
    try:
        current = load_candidate_orchestration(store, orchestration.candidate_sha)
        if current is None or current != orchestration:
            _invalid()
        checkpoint = load_candidate_fetch_stage_checkpoint(store, current.candidate_sha)
        if checkpoint is not None and checkpoint.phase == "completed":
            if current.phase != "staged" or current.next_action != "analyze":
                _invalid()
            destination = _completed_workspace(
                staging_root, home_assistant_root, checkpoint.workspace_id
            )
            stage = load_candidate_stage(destination)
            if (
                stage.target != checkpoint.target
                or stage.repository_id != checkpoint.repository_id
                or stage.commit_sha != checkpoint.candidate_sha
                or stage.manifest_sha256 != checkpoint.manifest_sha256
                or len(stage.entries) != checkpoint.entry_count
                or sum(entry.size for entry in stage.entries) != checkpoint.total_bytes
            ):
                _invalid()
            return CandidateFetchStageResult(checkpoint, current, True)
        if current.phase != "detected" or current.next_action != "fetch_stage":
            _invalid()
        if checkpoint is None:
            checkpoint = _record_plan(store, current, current_time)
        elif checkpoint.orchestration_sha256 != current.record_sha256:
            _invalid()

        destination = _completed_workspace(
            staging_root,
            home_assistant_root,
            checkpoint.workspace_id,
            allow_missing=True,
        )
        _clean_abandoned_stage_temps(staging_root)
        _remove_incomplete_destination(destination, staging_root)
        if token is None:
            raise CandidateFetchStageExecutionError(
                "Candidate Fetch/Stage credentials are unavailable", transient=True
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
        stage = stager(fetched, staging_root, home_assistant_root)
        verify_candidate_stage(stage)
        if (
            stage.target != current.target
            or stage.repository_id != current.repository_id
            or stage.commit_sha != current.candidate_sha
            or stage.branch != "candidate"
        ):
            _invalid()
        os.rename(stage.root, destination)
        moved = replace(
            stage,
            root=destination,
            tree=destination / "tree",
            manifest=destination / "manifest.json",
        )
        verify_candidate_stage(moved)
        _fsync_directory(destination)
        _fsync_directory(destination.parent)
        completed = checkpoint.complete(moved, completed_at=current_time)
        advanced = staged_candidate_orchestration(current, updated_at=current_time)
        _complete_atomically(store, checkpoint, completed, current, advanced)
        return CandidateFetchStageResult(completed, advanced, False)
    except CandidateFetchStageExecutionError:
        raise
    except CandidateFetchError as error:
        transient = "command failed" in str(error) or "fetch failed" in str(error)
        raise CandidateFetchStageExecutionError(
            "Candidate Fetch/Stage is temporarily unavailable"
            if transient
            else "Candidate Fetch/Stage evidence is invalid",
            transient=transient,
        ) from None
    except RepositoryVerificationError as error:
        message = str(error)
        transient = "transport failed" in message or any(
            f"HTTP {code}" in message for code in (408, 429, 500, 502, 503, 504)
        )
        raise CandidateFetchStageExecutionError(
            "Candidate Fetch/Stage is temporarily unavailable"
            if transient
            else "Candidate Fetch/Stage evidence is invalid",
            transient=transient,
        ) from None
    except CandidateStageError as error:
        transient = "filesystem operation failed" in str(error)
        raise CandidateFetchStageExecutionError(
            "Candidate Fetch/Stage is temporarily unavailable"
            if transient
            else "Candidate Fetch/Stage evidence is invalid",
            transient=transient,
        ) from None
    except (
        CandidateDetectionError,
        CandidateOrchestrationError,
        StateError,
        sqlite3.Error,
        OSError,
        ValueError,
    ):
        raise CandidateFetchStageExecutionError(
            "Candidate Fetch/Stage evidence is invalid", transient=False
        ) from None
    finally:
        if fetched is not None:
            _remove_fetch_workspace(fetched.root, workspace_root)


def load_candidate_fetch_stage_checkpoint(
    store: StateStore,
    candidate_sha: str,
) -> CandidateFetchStageCheckpoint | None:
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha, schema_version, orchestration_sha256, target, "
            "repository_id, workspace_id, phase, manifest_sha256, entry_count, "
            "total_bytes, planned_at, completed_at, record_sha256 "
            "FROM candidate_fetch_stage_checkpoint WHERE candidate_sha = ?",
            (candidate_sha,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid()
        result = CandidateFetchStageCheckpoint.from_database_row(rows[0])
        current = load_candidate_orchestration(store, result.candidate_sha)
        if (
            current is None
            or current.target != result.target
            or current.repository_id != result.repository_id
            or (result.phase == "planned" and current.record_sha256 != result.orchestration_sha256)
            or (result.phase == "completed" and current.phase != "staged")
        ):
            _invalid()
        return result
    except CandidateFetchStageExecutionError:
        raise
    except (CandidateOrchestrationError, StateError, sqlite3.Error):
        raise CandidateFetchStageExecutionError(
            "Candidate Fetch/Stage evidence is invalid", transient=False
        ) from None


def candidate_fetch_stage_runtime_evidence(
    store: StateStore,
) -> tuple[CandidateFetchStageRuntimeEvidence, ...]:
    """Read bounded checkpoint metadata without selecting candidate identities."""
    try:
        rows = store._connection.execute(
            "SELECT candidate_sha, schema_version, orchestration_sha256, target, "
            "repository_id, workspace_id, phase, manifest_sha256, entry_count, "
            "total_bytes, planned_at, completed_at, record_sha256 "
            "FROM candidate_fetch_stage_checkpoint ORDER BY planned_at, candidate_sha LIMIT ?",
            (_MAX_DISCOVERABLE + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE:
            _invalid()
        checkpoints = tuple(
            CandidateFetchStageCheckpoint.from_database_row(tuple(row)) for row in rows
        )
        if any(
            load_candidate_fetch_stage_checkpoint(store, checkpoint.candidate_sha) != checkpoint
            for checkpoint in checkpoints
        ):
            _invalid()
        return tuple(
            CandidateFetchStageRuntimeEvidence(
                phase=checkpoint.phase,
                entry_count=checkpoint.entry_count,
                total_bytes=checkpoint.total_bytes,
                planned_at=checkpoint.planned_at,
                completed_at=checkpoint.completed_at,
            )
            for checkpoint in checkpoints
        )
    except CandidateFetchStageExecutionError:
        raise
    except (StateError, sqlite3.Error):
        raise CandidateFetchStageExecutionError(
            "Candidate Fetch/Stage evidence is invalid", transient=False
        ) from None


def _record_plan(
    store: StateStore,
    orchestration: CandidateOrchestration,
    when: datetime,
) -> CandidateFetchStageCheckpoint:
    plan = CandidateFetchStageCheckpoint.plan(
        orchestration,
        workspace_id=str(uuid4()),
        planned_at=when,
    )
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        current = load_candidate_orchestration(store, orchestration.candidate_sha)
        if current != orchestration:
            _invalid()
        db.execute(
            "INSERT INTO candidate_fetch_stage_checkpoint "
            "(candidate_sha, schema_version, orchestration_sha256, target, repository_id, "
            "workspace_id, phase, manifest_sha256, entry_count, total_bytes, planned_at, "
            "completed_at, record_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            plan.database_values(),
        )
    persisted = load_candidate_fetch_stage_checkpoint(store, orchestration.candidate_sha)
    if persisted != plan:
        _invalid()
    return plan


def _complete_atomically(
    store: StateStore,
    planned: CandidateFetchStageCheckpoint,
    completed: CandidateFetchStageCheckpoint,
    current: CandidateOrchestration,
    advanced: CandidateOrchestration,
) -> None:
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        persisted = load_candidate_fetch_stage_checkpoint(store, planned.candidate_sha)
        orchestration = load_candidate_orchestration(store, planned.candidate_sha)
        if persisted != planned or orchestration != current:
            _invalid()
        checkpoint_result = db.execute(
            "UPDATE candidate_fetch_stage_checkpoint SET phase = ?, manifest_sha256 = ?, "
            "entry_count = ?, total_bytes = ?, completed_at = ?, record_sha256 = ? "
            "WHERE candidate_sha = ? AND phase = 'planned' AND record_sha256 = ?",
            (
                completed.phase,
                completed.manifest_sha256,
                completed.entry_count,
                completed.total_bytes,
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


def _completed_workspace(
    staging_root: Path,
    home_assistant_root: Path,
    workspace_id: str,
    *,
    allow_missing: bool = False,
) -> Path:
    try:
        if str(UUID(workspace_id)) != workspace_id:
            _invalid()
        staging = staging_root.resolve(strict=True)
        home = home_assistant_root.resolve(strict=True)
    except (OSError, ValueError):
        _invalid()
    if staging == home or staging in home.parents or home in staging.parents:
        _invalid()
    destination = staging / f"{_FINAL_PREFIX}{workspace_id}"
    if allow_missing and not os.path.lexists(destination):
        return destination
    try:
        metadata = destination.lstat()
    except OSError:
        _invalid()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or destination.is_symlink()
        or metadata.st_uid != os.geteuid()
    ):
        _invalid()
    return destination


def _remove_incomplete_destination(destination: Path, staging_root: Path) -> None:
    if not os.path.lexists(destination):
        return
    root = staging_root.resolve(strict=True)
    if destination.parent != root:
        _invalid()
    metadata = destination.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or destination.is_symlink()
        or metadata.st_uid != os.geteuid()
    ):
        _invalid()
    shutil.rmtree(destination)


def _clean_abandoned_stage_temps(staging_root: Path) -> None:
    try:
        root = staging_root.resolve(strict=True)
        candidates = sorted(
            entry
            for entry in root.iterdir()
            if entry.name.startswith(_TEMP_PREFIX) and entry.name.endswith(".tmp")
        )
        if len(candidates) > _MAX_DISCOVERABLE:
            _invalid()
        for candidate in candidates:
            metadata = candidate.lstat()
            if (
                candidate.parent != root
                or not stat.S_ISDIR(metadata.st_mode)
                or candidate.is_symlink()
                or metadata.st_uid != os.geteuid()
            ):
                _invalid()
            shutil.rmtree(candidate)
    except CandidateFetchStageExecutionError:
        raise
    except OSError:
        raise CandidateFetchStageExecutionError(
            "Candidate Fetch/Stage is temporarily unavailable", transient=True
        ) from None


def _remove_fetch_workspace(path: Path, workspace_root: Path) -> None:
    try:
        root = workspace_root.resolve(strict=True)
        metadata = path.lstat()
        if (
            path.resolve(strict=True).parent == root
            and stat.S_ISDIR(metadata.st_mode)
            and not path.is_symlink()
            and metadata.st_uid == os.geteuid()
        ):
            shutil.rmtree(path)
    except OSError:
        return


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(UTC)


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid()
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        _invalid()
    return value


def _invalid() -> NoReturn:
    raise CandidateFetchStageExecutionError(
        "Candidate Fetch/Stage evidence is invalid", transient=False
    )
