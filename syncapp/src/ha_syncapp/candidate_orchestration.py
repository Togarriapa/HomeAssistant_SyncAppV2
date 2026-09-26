"""Durable, fail-closed selection of the first candidate deployment action."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .state import StateError, StateStore, WorkItem

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_WORK_KIND = "candidate"
_SCHEMA_VERSION = 1
_MAX_DISCOVERABLE = 64


class CandidateOrchestrationError(RuntimeError):
    """Candidate orchestration state cannot safely authorize an action."""


@dataclass(frozen=True, slots=True)
class CandidateOrchestration:
    """Content-free authority for one exact detected candidate SHA."""

    candidate_sha: str
    schema_version: int
    target: str
    repository_id: int
    phase: str
    next_action: str
    registered_at: datetime
    updated_at: datetime
    record_sha256: str

    @classmethod
    def create(
        cls,
        *,
        candidate_sha: str,
        target: str,
        repository_id: int,
        registered_at: datetime,
    ) -> CandidateOrchestration:
        when = _timestamp(registered_at)
        values: tuple[object, ...] = (
            _WORK_KIND,
            candidate_sha,
            _SCHEMA_VERSION,
            target,
            repository_id,
            "detected",
            "fetch_stage",
            when.isoformat(),
            when.isoformat(),
        )
        result = cls(
            candidate_sha,
            _SCHEMA_VERSION,
            target,
            repository_id,
            "detected",
            "fetch_stage",
            when,
            when,
            _digest(values),
        )
        result.validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CandidateOrchestration:
        if len(row) != 10 or row[0] != _WORK_KIND or type(row[4]) is not int:
            _invalid()
        try:
            registered_at = datetime.fromisoformat(_text(row[7]))
            updated_at = datetime.fromisoformat(_text(row[8]))
        except ValueError:
            _invalid()
        result = cls(
            _text(row[1]),
            _integer(row[2]),
            _text(row[3]),
            _integer(row[4]),
            _text(row[5]),
            _text(row[6]),
            registered_at,
            updated_at,
            _text(row[9]),
        )
        result.validate()
        if result.database_values() != row:
            _invalid()
        return result

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values: tuple[object, ...] = (
            _WORK_KIND,
            self.candidate_sha,
            self.schema_version,
            self.target,
            self.repository_id,
            self.phase,
            self.next_action,
            self.registered_at.astimezone(UTC).isoformat(),
            self.updated_at.astimezone(UTC).isoformat(),
        )
        return (*values, _digest(values))

    def validate(self) -> None:
        terminal = self.phase in {"completed", "blocked"}
        if (
            _COMMIT.fullmatch(self.candidate_sha) is None
            or self.schema_version != _SCHEMA_VERSION
            or _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or not 0 < self.repository_id <= 2**63 - 1
            or self.phase
            not in {"detected", "staged", "integrity_verified", "completed", "blocked"}
            or self.next_action
            not in {"fetch_stage", "analyze", "analyze_dependencies", "none"}
            or (self.phase == "detected") != (self.next_action == "fetch_stage")
            or (self.phase == "staged") != (self.next_action == "analyze")
            or (self.phase == "integrity_verified")
            != (self.next_action == "analyze_dependencies")
            or terminal != (self.next_action == "none")
            or self.registered_at.tzinfo is None
            or self.registered_at.utcoffset() is None
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
            or self.updated_at < self.registered_at
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid()


@dataclass(frozen=True, slots=True)
class CandidateOrchestrationRuntimeEvidence:
    """Sanitized orchestration state that excludes repository and candidate identity."""

    phase: str
    next_action: str
    updated_at: datetime


def staged_candidate_orchestration(
    current: CandidateOrchestration,
    *,
    updated_at: datetime,
) -> CandidateOrchestration:
    """Derive the sole permitted Fetch/Stage successor without persisting it."""
    current.validate()
    if current.phase != "detected" or current.next_action != "fetch_stage":
        _invalid()
    when = _timestamp(updated_at)
    values: tuple[object, ...] = (
        _WORK_KIND,
        current.candidate_sha,
        current.schema_version,
        current.target,
        current.repository_id,
        "staged",
        "analyze",
        current.registered_at.astimezone(UTC).isoformat(),
        when.isoformat(),
    )
    result = CandidateOrchestration(
        candidate_sha=current.candidate_sha,
        schema_version=current.schema_version,
        target=current.target,
        repository_id=current.repository_id,
        phase="staged",
        next_action="analyze",
        registered_at=current.registered_at,
        updated_at=when,
        record_sha256=_digest(values),
    )
    result.validate()
    return result


def integrity_verified_candidate_orchestration(
    current: CandidateOrchestration,
    *,
    updated_at: datetime,
) -> CandidateOrchestration:
    """Derive the sole permitted integrity-analysis successor without persisting it."""
    current.validate()
    if current.phase != "staged" or current.next_action != "analyze":
        _invalid()
    when = _timestamp(updated_at)
    values: tuple[object, ...] = (
        _WORK_KIND,
        current.candidate_sha,
        current.schema_version,
        current.target,
        current.repository_id,
        "integrity_verified",
        "analyze_dependencies",
        current.registered_at.astimezone(UTC).isoformat(),
        when.isoformat(),
    )
    result = CandidateOrchestration(
        candidate_sha=current.candidate_sha,
        schema_version=current.schema_version,
        target=current.target,
        repository_id=current.repository_id,
        phase="integrity_verified",
        next_action="analyze_dependencies",
        registered_at=current.registered_at,
        updated_at=when,
        record_sha256=_digest(values),
    )
    result.validate()
    return result


def register_claimed_candidate(
    store: StateStore,
    claimed: WorkItem,
    *,
    target: str,
    repository_id: int,
    now: datetime | None = None,
) -> CandidateOrchestration:
    """Bind one atomically claimed candidate to its first non-mutating action."""
    if type(store) is not StateStore or type(claimed) is not WorkItem:
        raise CandidateOrchestrationError("Candidate orchestration authority is unavailable")
    try:
        record = CandidateOrchestration.create(
            candidate_sha=claimed.work_key,
            target=target,
            repository_id=repository_id,
            registered_at=datetime.now(UTC) if now is None else now,
        )
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT work_kind, work_key, status, attempts, created_at, updated_at, "
                "next_attempt_at FROM work WHERE work_kind = ? AND work_key = ?",
                (_WORK_KIND, claimed.work_key),
            ).fetchall()
            if len(rows) != 1 or store._work_from_row(rows[0]) != claimed:
                raise CandidateOrchestrationError(
                    "Candidate orchestration authority is unavailable"
                )
            if claimed.work_kind != _WORK_KIND or claimed.status != "running":
                raise CandidateOrchestrationError(
                    "Candidate orchestration authority is unavailable"
                )
            if store.repository_id(target) != repository_id:
                raise CandidateOrchestrationError(
                    "Candidate orchestration authority is unavailable"
                )
            existing = _load(store, claimed.work_key)
            if existing is not None:
                if (
                    existing.target != target
                    or existing.repository_id != repository_id
                    or existing.candidate_sha != claimed.work_key
                ):
                    _invalid()
                return existing
            db.execute(
                "INSERT INTO candidate_orchestration "
                "(work_kind, candidate_sha, schema_version, target, repository_id, phase, "
                "next_action, registered_at, updated_at, record_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                record.database_values(),
            )
            persisted = _load(store, claimed.work_key)
            if persisted is None:
                _invalid()
            return persisted
    except CandidateOrchestrationError:
        raise
    except (StateError, sqlite3.Error, ValueError, TypeError):
        raise CandidateOrchestrationError(
            "Candidate orchestration authority is unavailable"
        ) from None


def load_candidate_orchestration(
    store: StateStore, candidate_sha: str
) -> CandidateOrchestration | None:
    """Load and revalidate one exact candidate authority without external I/O."""
    try:
        if type(store) is not StateStore or _COMMIT.fullmatch(candidate_sha) is None:
            _invalid()
        record = _load(store, candidate_sha)
        if record is not None and store.repository_id(record.target) != record.repository_id:
            _invalid()
        return record
    except CandidateOrchestrationError:
        raise
    except (StateError, sqlite3.Error, TypeError):
        raise CandidateOrchestrationError("Candidate orchestration state is invalid") from None


def discover_candidate_orchestrations(
    store: StateStore,
) -> tuple[CandidateOrchestration, ...]:
    """Return bounded nonterminal authorities for Retrigger; execute no action."""
    try:
        rows = store._connection.execute(
            "SELECT work_kind, candidate_sha, schema_version, target, repository_id, phase, "
            "next_action, registered_at, updated_at, record_sha256 "
            "FROM candidate_orchestration WHERE next_action != 'none' "
            "ORDER BY registered_at, candidate_sha LIMIT ?",
            (_MAX_DISCOVERABLE + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE:
            _invalid()
        records = tuple(CandidateOrchestration.from_database_row(row) for row in rows)
        if any(store.repository_id(record.target) != record.repository_id for record in records):
            _invalid()
        return records
    except CandidateOrchestrationError:
        raise
    except (StateError, sqlite3.Error):
        raise CandidateOrchestrationError("Candidate orchestration state is invalid") from None


def candidate_orchestration_runtime_evidence(
    store: StateStore,
) -> tuple[CandidateOrchestrationRuntimeEvidence, ...]:
    """Expose bounded content-free state for runtime and AI diagnostics."""
    try:
        rows = store._connection.execute(
            "SELECT work_kind, candidate_sha, schema_version, target, repository_id, phase, "
            "next_action, registered_at, updated_at, record_sha256 "
            "FROM candidate_orchestration ORDER BY updated_at, phase LIMIT ?",
            (_MAX_DISCOVERABLE + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE:
            _invalid()
        records = tuple(CandidateOrchestration.from_database_row(row) for row in rows)
        if any(store.repository_id(record.target) != record.repository_id for record in records):
            _invalid()
        return tuple(
            CandidateOrchestrationRuntimeEvidence(
                phase=record.phase,
                next_action=record.next_action,
                updated_at=record.updated_at,
            )
            for record in records
        )
    except CandidateOrchestrationError:
        raise
    except (StateError, sqlite3.Error):
        raise CandidateOrchestrationError("Candidate orchestration state is invalid") from None


def _load(store: StateStore, candidate_sha: str) -> CandidateOrchestration | None:
    rows = store._connection.execute(
        "SELECT work_kind, candidate_sha, schema_version, target, repository_id, phase, "
        "next_action, registered_at, updated_at, record_sha256 "
        "FROM candidate_orchestration WHERE candidate_sha = ?",
        (candidate_sha,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        _invalid()
    return CandidateOrchestration.from_database_row(rows[0])


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(UTC)


def _digest(values: tuple[object, ...]) -> str:
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid()
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        _invalid()
    return value


def _invalid() -> NoReturn:
    raise CandidateOrchestrationError("Candidate orchestration state is invalid")
