"""Crash-safe candidate backup mutation journal and exact evidence binding."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import NoReturn
from uuid import NAMESPACE_URL, uuid5

from .candidate_backup import CandidateBackupEvidence
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_CORE_RELEASE = re.compile(r"^20[0-9]{2}\.(?:[1-9]|1[0-2])\.(?:0|[1-9][0-9]*)$")
_BACKUP_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SCHEMA_VERSION = 1
_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
_PHASES = frozenset({"planned", "mutation_started", "uncertain", "completed", "blocked"})


class CandidateBackupCheckpointError(RuntimeError):
    """Persisted candidate backup evidence is unsafe or corrupt."""


def candidate_backup_deployment_id(target: str, repository_id: int, candidate_sha: str) -> str:
    """Return the stable UUID for one exact repository candidate."""
    if (
        not isinstance(target, str)
        or _TARGET.fullmatch(target) is None
        or type(repository_id) is not int
        or not 0 < repository_id <= 2**63 - 1
        or not isinstance(candidate_sha, str)
        or _COMMIT.fullmatch(candidate_sha) is None
    ):
        _invalid()
    return str(uuid5(NAMESPACE_URL, f"syncapp:{target}:{repository_id}:{candidate_sha}"))


@dataclass(frozen=True, slots=True)
class CandidateBackupCheckpoint:
    candidate_sha: str
    schema_version: int
    orchestration_sha256: str
    fetch_stage_sha256: str
    integrity_sha256: str
    dependency_sha256: str
    risk_sha256: str
    static_sha256: str
    semantic_sha256: str
    target: str
    repository_id: int
    baseline_sha: str
    stage_manifest_sha256: str
    runtime_sha256: str
    risk_level: str
    core_version: str
    deployment_id: str
    request_name: str
    phase: str
    backup_slug: str | None
    planned_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    record_sha256: str

    @classmethod
    def plan(
        cls,
        *,
        candidate_sha: str,
        orchestration_sha256: str,
        fetch_stage_sha256: str,
        integrity_sha256: str,
        dependency_sha256: str,
        risk_sha256: str,
        static_sha256: str,
        semantic_sha256: str,
        target: str,
        repository_id: int,
        baseline_sha: str,
        stage_manifest_sha256: str,
        runtime_sha256: str,
        risk_level: str,
        core_version: str,
        planned_at: datetime,
    ) -> CandidateBackupCheckpoint:
        deployment_id = candidate_backup_deployment_id(target, repository_id, candidate_sha)
        result = cls(
            candidate_sha,
            _SCHEMA_VERSION,
            orchestration_sha256,
            fetch_stage_sha256,
            integrity_sha256,
            dependency_sha256,
            risk_sha256,
            static_sha256,
            semantic_sha256,
            target,
            repository_id,
            baseline_sha,
            stage_manifest_sha256,
            runtime_sha256,
            risk_level,
            core_version,
            deployment_id,
            f"SyncApp candidate {candidate_sha[:12]} {deployment_id}",
            "planned",
            None,
            _timestamp(planned_at),
            None,
            None,
            "0" * 64,
        )
        return result._redigest()

    def start(self, *, started_at: datetime) -> CandidateBackupCheckpoint:
        self.validate()
        if self.phase != "planned":
            _invalid()
        when = _timestamp(started_at)
        if when < self.planned_at:
            _invalid()
        return replace(self, phase="mutation_started", started_at=when)._redigest()

    def mark_uncertain(self) -> CandidateBackupCheckpoint:
        self.validate()
        if self.phase != "mutation_started":
            _invalid()
        return replace(self, phase="uncertain")._redigest()

    def complete(
        self, evidence: CandidateBackupEvidence, *, completed_at: datetime
    ) -> CandidateBackupCheckpoint:
        self.validate()
        if (
            self.phase not in {"mutation_started", "uncertain"}
            or type(evidence) is not CandidateBackupEvidence
        ):
            _invalid()
        expected = CandidateBackupEvidence(
            target=self.target,
            repository_id=self.repository_id,
            baseline_sha=self.baseline_sha,
            candidate_sha=self.candidate_sha,
            stage_manifest_sha256=self.stage_manifest_sha256,
            runtime_sha256=self.runtime_sha256,
            risk_level=self.risk_level,
            core_version=self.core_version,
            backup_slug=evidence.backup_slug,
        )
        if evidence != expected or _BACKUP_SLUG.fullmatch(evidence.backup_slug) is None:
            _invalid()
        when = _timestamp(completed_at)
        if self.started_at is None or when < self.started_at:
            _invalid()
        return replace(
            self, phase="completed", backup_slug=evidence.backup_slug, completed_at=when
        )._redigest()

    def block(self, *, completed_at: datetime) -> CandidateBackupCheckpoint:
        self.validate()
        if self.phase not in {"planned", "uncertain"}:
            _invalid()
        when = _timestamp(completed_at)
        if when < self.planned_at:
            _invalid()
        return replace(self, phase="blocked", completed_at=when)._redigest()

    def evidence(self) -> CandidateBackupEvidence:
        self.validate()
        if self.phase != "completed" or self.backup_slug is None:
            _invalid()
        return CandidateBackupEvidence(
            target=self.target,
            repository_id=self.repository_id,
            baseline_sha=self.baseline_sha,
            candidate_sha=self.candidate_sha,
            stage_manifest_sha256=self.stage_manifest_sha256,
            runtime_sha256=self.runtime_sha256,
            risk_level=self.risk_level,
            core_version=self.core_version,
            backup_slug=self.backup_slug,
        )

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CandidateBackupCheckpoint:
        if len(row) != 24:
            _invalid()
        try:
            result = cls(
                _text(row[0]),
                _integer(row[1]),
                _text(row[2]),
                _text(row[3]),
                _text(row[4]),
                _text(row[5]),
                _text(row[6]),
                _text(row[7]),
                _text(row[8]),
                _text(row[9]),
                _integer(row[10]),
                _text(row[11]),
                _text(row[12]),
                _text(row[13]),
                _text(row[14]),
                _text(row[15]),
                _text(row[16]),
                _text(row[17]),
                _text(row[18]),
                None if row[19] is None else _text(row[19]),
                datetime.fromisoformat(_text(row[20])),
                None if row[21] is None else datetime.fromisoformat(_text(row[21])),
                None if row[22] is None else datetime.fromisoformat(_text(row[22])),
                _text(row[23]),
            )
        except ValueError:
            _invalid()
        result.validate()
        if result.database_values() != row:
            _invalid()
        return result

    def values_without_digest(self) -> tuple[object, ...]:
        return (
            self.candidate_sha,
            self.schema_version,
            self.orchestration_sha256,
            self.fetch_stage_sha256,
            self.integrity_sha256,
            self.dependency_sha256,
            self.risk_sha256,
            self.static_sha256,
            self.semantic_sha256,
            self.target,
            self.repository_id,
            self.baseline_sha,
            self.stage_manifest_sha256,
            self.runtime_sha256,
            self.risk_level,
            self.core_version,
            self.deployment_id,
            self.request_name,
            self.phase,
            self.backup_slug,
            self.planned_at.astimezone(UTC).isoformat(),
            None if self.started_at is None else self.started_at.astimezone(UTC).isoformat(),
            None if self.completed_at is None else self.completed_at.astimezone(UTC).isoformat(),
        )

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values = self.values_without_digest()
        return (*values, _digest(values))

    def validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid()
        expected_id = candidate_backup_deployment_id(
            self.target, self.repository_id, self.candidate_sha
        )
        expected_name = f"SyncApp candidate {self.candidate_sha[:12]} {expected_id}"
        phase_requires_start = self.phase in {"mutation_started", "uncertain", "completed"}
        terminal = self.phase in {"completed", "blocked"}
        if (
            _COMMIT.fullmatch(self.candidate_sha) is None
            or self.schema_version != _SCHEMA_VERSION
            or any(
                _HASH.fullmatch(value) is None
                for value in (
                    self.orchestration_sha256,
                    self.fetch_stage_sha256,
                    self.integrity_sha256,
                    self.dependency_sha256,
                    self.risk_sha256,
                    self.static_sha256,
                    self.semantic_sha256,
                    self.stage_manifest_sha256,
                    self.runtime_sha256,
                    self.record_sha256,
                )
            )
            or _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or not 0 < self.repository_id <= 2**63 - 1
            or _OBJECT_ID.fullmatch(self.baseline_sha) is None
            or self.baseline_sha == self.candidate_sha
            or self.risk_level not in _RISK_LEVELS
            or _CORE_RELEASE.fullmatch(self.core_version) is None
            or self.deployment_id != expected_id
            or self.request_name != expected_name
            or self.phase not in _PHASES
            or (phase_requires_start and self.started_at is None)
            or (self.phase == "planned" and self.started_at is not None)
            or terminal != (self.completed_at is not None)
            or (self.phase == "completed") != (self.backup_slug is not None)
            or (self.backup_slug is not None and _BACKUP_SLUG.fullmatch(self.backup_slug) is None)
            or self.planned_at.tzinfo is None
            or self.planned_at.utcoffset() is None
            or (self.started_at is not None and self.started_at < self.planned_at)
            or (
                self.completed_at is not None
                and self.completed_at < (self.started_at or self.planned_at)
            )
            or self.record_sha256 != _digest(self.values_without_digest())
        ):
            _invalid()

    def _redigest(self) -> CandidateBackupCheckpoint:
        result = replace(self, record_sha256="0" * 64)
        result = replace(result, record_sha256=_digest(result.values_without_digest()))
        result.validate()
        return result


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(UTC)


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid()
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        _invalid()
    return value


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _invalid() -> NoReturn:
    raise CandidateBackupCheckpointError("Candidate backup checkpoint is invalid")
