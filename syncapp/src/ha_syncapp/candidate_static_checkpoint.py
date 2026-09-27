"""Canonical durable static-validation evidence for one exact candidate."""

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
from .candidate_stage import CandidateStage
from .candidate_validation import (
    CandidateStaticValidation,
    CandidateValidationError,
    CandidateValidationFile,
    verify_candidate_static_validation,
)
from .runtime_inventory import RuntimeInventoryInput

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SCHEMA_VERSION = 1
_MAX_EVIDENCE_BYTES = 32 * 1024 * 1024


class CandidateStaticCheckpointError(RuntimeError):
    """Persisted static-validation evidence is unsafe or corrupt."""


@dataclass(frozen=True, slots=True)
class CandidateStaticCheckpoint:
    candidate_sha: str
    schema_version: int
    orchestration_sha256: str
    fetch_stage_sha256: str
    integrity_sha256: str
    dependency_sha256: str
    risk_sha256: str
    target: str
    repository_id: int
    baseline_sha: str
    stage_manifest_sha256: str
    phase: str
    validation_json: str | None
    syntax_valid: bool | None
    invalid_count: int | None
    unvalidated_count: int | None
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
        target: str,
        repository_id: int,
        baseline_sha: str,
        stage_manifest_sha256: str,
        planned_at: datetime,
    ) -> CandidateStaticCheckpoint:
        result = cls(
            candidate_sha,
            _SCHEMA_VERSION,
            orchestration_sha256,
            fetch_stage_sha256,
            integrity_sha256,
            dependency_sha256,
            risk_sha256,
            target,
            repository_id,
            baseline_sha,
            stage_manifest_sha256,
            "planned",
            None,
            None,
            None,
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
        validation: CandidateStaticValidation,
        integrity: CandidateIntegrity,
        stage: CandidateStage,
        dependencies: CandidateDependencyAnalysis,
        impact: CandidateImpactAnalysis,
        risk: CandidateRiskClassification,
        runtime: RuntimeInventoryInput,
        *,
        completed_at: datetime,
    ) -> CandidateStaticCheckpoint:
        self.validate()
        if self.phase != "planned":
            _invalid()
        try:
            verify_candidate_static_validation(
                validation, integrity, stage, dependencies, impact, risk, runtime
            )
        except CandidateValidationError:
            _invalid()
        if (
            validation.target != self.target
            or validation.repository_id != self.repository_id
            or validation.baseline_sha != self.baseline_sha
            or validation.candidate_sha != self.candidate_sha
            or validation.stage_manifest_sha256 != self.stage_manifest_sha256
        ):
            _invalid()
        result = replace(
            self,
            phase="completed",
            validation_json=_serialize(validation),
            syntax_valid=validation.syntax_valid,
            invalid_count=len(validation.invalid_paths),
            unvalidated_count=len(validation.unvalidated_paths),
            completed_at=_timestamp(completed_at),
            record_sha256="0" * 64,
        )
        result = replace(result, record_sha256=_digest(result.values_without_digest()))
        result.validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CandidateStaticCheckpoint:
        if len(row) != 19 or type(row[8]) is not int:
            _invalid()
        try:
            planned = datetime.fromisoformat(_text(row[16]))
            completed = None if row[17] is None else datetime.fromisoformat(_text(row[17]))
        except ValueError:
            _invalid()
        result = cls(
            _text(row[0]),
            _integer(row[1]),
            _text(row[2]),
            _text(row[3]),
            _text(row[4]),
            _text(row[5]),
            _text(row[6]),
            _text(row[7]),
            _integer(row[8]),
            _text(row[9]),
            _text(row[10]),
            _text(row[11]),
            None if row[12] is None else _text(row[12]),
            None if row[13] is None else _boolean(row[13]),
            None if row[14] is None else _integer(row[14]),
            None if row[15] is None else _integer(row[15]),
            planned,
            completed,
            _text(row[18]),
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
            self.fetch_stage_sha256,
            self.integrity_sha256,
            self.dependency_sha256,
            self.risk_sha256,
            self.target,
            self.repository_id,
            self.baseline_sha,
            self.stage_manifest_sha256,
            self.phase,
            self.validation_json,
            self.syntax_valid,
            self.invalid_count,
            self.unvalidated_count,
            self.planned_at.astimezone(UTC).isoformat(),
            None if self.completed_at is None else self.completed_at.astimezone(UTC).isoformat(),
        )

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values = self.values_without_digest()
        return (*values, _digest(values))

    def validation(self) -> CandidateStaticValidation:
        self.validate()
        if self.validation_json is None:
            _invalid()
        return _parse_validation(self.validation_json)

    def validate(self) -> None:
        completed = self.phase == "completed"
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
                    self.stage_manifest_sha256,
                )
            )
            or _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or _OBJECT_ID.fullmatch(self.baseline_sha) is None
            or self.phase not in {"planned", "completed"}
            or completed != (self.validation_json is not None)
            or completed != (self.syntax_valid is not None)
            or completed != (self.invalid_count is not None)
            or completed != (self.unvalidated_count is not None)
            or completed != (self.completed_at is not None)
            or (self.syntax_valid is not None and type(self.syntax_valid) is not bool)
            or (self.invalid_count is not None and self.invalid_count < 0)
            or (self.unvalidated_count is not None and self.unvalidated_count < 0)
            or self.planned_at.tzinfo is None
            or (self.completed_at is not None and self.completed_at < self.planned_at)
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid()
        if completed:
            if (
                self.validation_json is None
                or len(self.validation_json.encode()) > _MAX_EVIDENCE_BYTES
            ):
                _invalid()
            result = _parse_validation(self.validation_json)
            if (
                _serialize(result) != self.validation_json
                or result.syntax_valid is not self.syntax_valid
                or len(result.invalid_paths) != self.invalid_count
                or len(result.unvalidated_paths) != self.unvalidated_count
                or result.target != self.target
                or result.repository_id != self.repository_id
                or result.baseline_sha != self.baseline_sha
                or result.candidate_sha != self.candidate_sha
                or result.stage_manifest_sha256 != self.stage_manifest_sha256
            ):
                _invalid()
        if self.record_sha256 != _digest(self.values_without_digest()):
            _invalid()


def _serialize(result: CandidateStaticValidation) -> str:
    return json.dumps(asdict(result), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _parse_validation(raw: str) -> CandidateStaticValidation:
    try:
        data = json.loads(raw)
        files = tuple(
            CandidateValidationFile(
                path=item["path"],
                status=item["status"],
                format=item["format"],
                reasons=tuple(item["reasons"]),
            )
            for item in data.pop("files")
        )
        data["invalid_paths"] = tuple(data["invalid_paths"])
        data["unvalidated_paths"] = tuple(data["unvalidated_paths"])
        result = CandidateStaticValidation(**data, files=files)
        # Reuse the public verifier's structural validation through canonical round-trip.
        if _serialize(result) != raw:
            _invalid()
        return result
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _invalid()


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(UTC)


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _text(value: object) -> str:
    if type(value) is not str:
        _invalid()
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        _invalid()
    return value


def _boolean(value: object) -> bool:
    if type(value) is bool:
        return value
    if type(value) is not int or value not in {0, 1}:
        _invalid()
    return bool(value)


def _invalid() -> NoReturn:
    raise CandidateStaticCheckpointError("candidate static checkpoint is invalid")
