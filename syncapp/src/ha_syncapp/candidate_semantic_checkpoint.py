"""Canonical durable exact-version semantic-validation evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_dependencies import CandidateDependencyAnalysis
from .candidate_impact import CandidateImpactAnalysis
from .candidate_integrity import CandidateIntegrity
from .candidate_risk import CandidateRiskClassification
from .candidate_semantics import (
    CandidateSemanticError,
    CandidateSemanticValidation,
    verify_candidate_semantic_validation,
)
from .candidate_stage import CandidateStage
from .candidate_validation import CandidateStaticValidation
from .core_version_evidence import CoreVersionEvidence
from .runtime_inventory import RuntimeInventoryInput

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_CORE_RELEASE = re.compile(r"^20[0-9]{2}\.(?:[1-9]|1[0-2])\.(?:0|[1-9][0-9]*)$")
_SCHEMA_VERSION = 1
_MAX_JSON_BYTES = 4096


class CandidateSemanticCheckpointError(RuntimeError):
    """Persisted semantic-validation evidence is unsafe or corrupt."""


@dataclass(frozen=True, slots=True)
class CandidateSemanticCheckpoint:
    candidate_sha: str
    schema_version: int
    orchestration_sha256: str
    fetch_stage_sha256: str
    integrity_sha256: str
    dependency_sha256: str
    risk_sha256: str
    static_sha256: str
    target: str
    repository_id: int
    baseline_sha: str
    stage_manifest_sha256: str
    runtime_sha256: str
    core_version: str
    phase: str
    semantic_json: str | None
    planned_at: datetime
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
        target: str,
        repository_id: int,
        baseline_sha: str,
        stage_manifest_sha256: str,
        runtime_sha256: str,
        core_version: str,
        planned_at: datetime,
    ) -> CandidateSemanticCheckpoint:
        result = cls(
            candidate_sha,
            _SCHEMA_VERSION,
            orchestration_sha256,
            fetch_stage_sha256,
            integrity_sha256,
            dependency_sha256,
            risk_sha256,
            static_sha256,
            target,
            repository_id,
            baseline_sha,
            stage_manifest_sha256,
            runtime_sha256,
            core_version,
            "planned",
            None,
            _timestamp(planned_at),
            None,
            "0" * 64,
        )
        result = replace(result, record_sha256=_digest(result.values_without_digest()))
        result.validate()
        return result

    def complete(
        self,
        semantic: CandidateSemanticValidation,
        static: CandidateStaticValidation,
        integrity: CandidateIntegrity,
        stage: CandidateStage,
        dependencies: CandidateDependencyAnalysis,
        impact: CandidateImpactAnalysis,
        risk: CandidateRiskClassification,
        runtime: RuntimeInventoryInput,
        version: CoreVersionEvidence,
        *,
        completed_at: datetime,
    ) -> CandidateSemanticCheckpoint:
        self.validate()
        if self.phase != "planned":
            _invalid()
        try:
            verify_candidate_semantic_validation(
                semantic, static, integrity, stage, dependencies, impact, risk, runtime, version
            )
        except CandidateSemanticError:
            _invalid()
        if (
            semantic.target != self.target
            or semantic.repository_id != self.repository_id
            or semantic.baseline_sha != self.baseline_sha
            or semantic.candidate_sha != self.candidate_sha
            or semantic.stage_manifest_sha256 != self.stage_manifest_sha256
            or semantic.runtime_sha256 != self.runtime_sha256
            or semantic.core_version != self.core_version
        ):
            _invalid()
        result = replace(
            self,
            phase="completed",
            semantic_json=json.dumps(asdict(semantic), sort_keys=True, separators=(",", ":")),
            completed_at=_timestamp(completed_at),
            record_sha256="0" * 64,
        )
        result = replace(result, record_sha256=_digest(result.values_without_digest()))
        result.validate()
        return result

    def block(self, *, completed_at: datetime) -> CandidateSemanticCheckpoint:
        self.validate()
        if self.phase != "planned":
            _invalid()
        result = replace(
            self,
            phase="blocked",
            completed_at=_timestamp(completed_at),
            record_sha256="0" * 64,
        )
        result = replace(result, record_sha256=_digest(result.values_without_digest()))
        result.validate()
        return result

    def semantic(self) -> CandidateSemanticValidation:
        self.validate()
        if self.phase != "completed" or self.semantic_json is None:
            _invalid()
        try:
            payload = json.loads(self.semantic_json)
            if not isinstance(payload, dict) or set(payload) != {
                "target",
                "repository_id",
                "baseline_sha",
                "candidate_sha",
                "stage_manifest_sha256",
                "runtime_sha256",
                "risk_level",
                "core_version",
                "validator",
            }:
                _invalid()
            result = CandidateSemanticValidation(**payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            _invalid()
        if (
            result.target != self.target
            or result.repository_id != self.repository_id
            or result.baseline_sha != self.baseline_sha
            or result.candidate_sha != self.candidate_sha
            or result.stage_manifest_sha256 != self.stage_manifest_sha256
            or result.runtime_sha256 != self.runtime_sha256
            or result.core_version != self.core_version
        ):
            _invalid()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CandidateSemanticCheckpoint:
        if len(row) != 19:
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
                _integer(row[9]),
                _text(row[10]),
                _text(row[11]),
                _text(row[12]),
                _text(row[13]),
                _text(row[14]),
                None if row[15] is None else _text(row[15]),
                datetime.fromisoformat(_text(row[16])),
                None if row[17] is None else datetime.fromisoformat(_text(row[17])),
                _text(row[18]),
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
            self.target,
            self.repository_id,
            self.baseline_sha,
            self.stage_manifest_sha256,
            self.runtime_sha256,
            self.core_version,
            self.phase,
            self.semantic_json,
            self.planned_at.astimezone(UTC).isoformat(),
            None if self.completed_at is None else self.completed_at.astimezone(UTC).isoformat(),
        )

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values = self.values_without_digest()
        return (*values, _digest(values))

    def validate(self) -> None:
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
                    self.stage_manifest_sha256,
                    self.runtime_sha256,
                    self.record_sha256,
                )
            )
            or _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or _OBJECT_ID.fullmatch(self.baseline_sha) is None
            or _CORE_RELEASE.fullmatch(self.core_version) is None
            or self.phase not in {"planned", "completed", "blocked"}
            or (self.phase == "completed") != (self.semantic_json is not None)
            or (
                self.semantic_json is not None
                and len(self.semantic_json.encode()) > _MAX_JSON_BYTES
            )
            or terminal != (self.completed_at is not None)
            or self.planned_at.tzinfo is None
            or self.planned_at.utcoffset() is None
            or (self.completed_at is not None and self.completed_at < self.planned_at)
            or self.record_sha256 != _digest(self.values_without_digest())
        ):
            _invalid()


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
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
    raise CandidateSemanticCheckpointError("Candidate semantic checkpoint is invalid")
