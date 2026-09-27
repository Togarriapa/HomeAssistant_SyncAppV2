"""Canonical durable impact and risk evidence for one exact candidate."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_dependencies import CandidateDependencyAnalysis
from .candidate_impact import (
    CandidateEntityImpact,
    CandidateImpactAnalysis,
    CandidateImpactError,
    verify_candidate_impact_analysis,
)
from .candidate_risk import (
    CandidateRiskClassification,
    CandidateRiskError,
    verify_candidate_risk_classification,
)
from .runtime_inventory import RuntimeInventoryInput

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SCHEMA_VERSION = 1
_MAX_EVIDENCE_BYTES = 32 * 1024 * 1024


class CandidateRiskCheckpointError(RuntimeError):
    """Persisted candidate impact/risk evidence is unsafe or corrupt."""


@dataclass(frozen=True, slots=True)
class CandidateRiskCheckpoint:
    candidate_sha: str
    schema_version: int
    orchestration_sha256: str
    dependency_sha256: str
    target: str
    repository_id: int
    baseline_sha: str
    stage_manifest_sha256: str
    phase: str
    impact_json: str | None
    risk_json: str | None
    risk_level: str | None
    affected_count: int | None
    planned_at: datetime
    completed_at: datetime | None
    record_sha256: str

    @classmethod
    def plan(
        cls,
        *,
        candidate_sha: str,
        orchestration_sha256: str,
        dependency_sha256: str,
        target: str,
        repository_id: int,
        baseline_sha: str,
        stage_manifest_sha256: str,
        planned_at: datetime,
    ) -> CandidateRiskCheckpoint:
        result = cls(
            candidate_sha,
            _SCHEMA_VERSION,
            orchestration_sha256,
            dependency_sha256,
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
        impact: CandidateImpactAnalysis,
        risk: CandidateRiskClassification,
        dependencies: CandidateDependencyAnalysis,
        runtime: RuntimeInventoryInput,
        *,
        completed_at: datetime,
    ) -> CandidateRiskCheckpoint:
        self.validate()
        if self.phase != "planned":
            _invalid()
        try:
            verify_candidate_impact_analysis(impact, dependencies, runtime)
            verify_candidate_risk_classification(risk, dependencies, impact, runtime)
        except (CandidateImpactError, CandidateRiskError):
            _invalid()
        if (
            risk.target,
            risk.repository_id,
            risk.baseline_sha,
            risk.candidate_sha,
            risk.stage_manifest_sha256,
        ) != (
            self.target,
            self.repository_id,
            self.baseline_sha,
            self.candidate_sha,
            self.stage_manifest_sha256,
        ):
            _invalid()
        result = replace(
            self,
            phase="completed",
            impact_json=_serialize(impact),
            risk_json=_serialize(risk),
            risk_level=risk.level,
            affected_count=len(risk.affected_entities),
            completed_at=_timestamp(completed_at),
            record_sha256="0" * 64,
        )
        result = replace(result, record_sha256=_digest(result.values_without_digest()))
        result.validate(dependencies, runtime)
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CandidateRiskCheckpoint:
        if len(row) != 16 or type(row[5]) is not int:
            _invalid()
        try:
            planned = datetime.fromisoformat(_text(row[13]))
            completed = None if row[14] is None else datetime.fromisoformat(_text(row[14]))
        except ValueError:
            _invalid()
        result = cls(
            _text(row[0]),
            _integer(row[1]),
            _text(row[2]),
            _text(row[3]),
            _text(row[4]),
            _integer(row[5]),
            _text(row[6]),
            _text(row[7]),
            _text(row[8]),
            None if row[9] is None else _text(row[9]),
            None if row[10] is None else _text(row[10]),
            None if row[11] is None else _text(row[11]),
            None if row[12] is None else _integer(row[12]),
            planned,
            completed,
            _text(row[15]),
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
            self.dependency_sha256,
            self.target,
            self.repository_id,
            self.baseline_sha,
            self.stage_manifest_sha256,
            self.phase,
            self.impact_json,
            self.risk_json,
            self.risk_level,
            self.affected_count,
            self.planned_at.astimezone(UTC).isoformat(),
            None if self.completed_at is None else self.completed_at.astimezone(UTC).isoformat(),
        )

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values = self.values_without_digest()
        return (*values, _digest(values))

    def impact(
        self, dependencies: CandidateDependencyAnalysis, runtime: RuntimeInventoryInput
    ) -> CandidateImpactAnalysis:
        if self.impact_json is None:
            _invalid()
        result = _parse_impact(self.impact_json)
        try:
            verify_candidate_impact_analysis(result, dependencies, runtime)
        except CandidateImpactError:
            _invalid()
        return result

    def risk(
        self, dependencies: CandidateDependencyAnalysis, runtime: RuntimeInventoryInput
    ) -> CandidateRiskClassification:
        impact = self.impact(dependencies, runtime)
        if self.risk_json is None:
            _invalid()
        result = _parse_risk(self.risk_json)
        try:
            verify_candidate_risk_classification(result, dependencies, impact, runtime)
        except CandidateRiskError:
            _invalid()
        return result

    def validate(
        self,
        dependencies: CandidateDependencyAnalysis | None = None,
        runtime: RuntimeInventoryInput | None = None,
    ) -> None:
        completed = self.phase == "completed"
        if (
            _COMMIT.fullmatch(self.candidate_sha) is None
            or self.schema_version != _SCHEMA_VERSION
            or _HASH.fullmatch(self.orchestration_sha256) is None
            or _HASH.fullmatch(self.dependency_sha256) is None
            or _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or _OBJECT_ID.fullmatch(self.baseline_sha) is None
            or _HASH.fullmatch(self.stage_manifest_sha256) is None
            or self.phase not in {"planned", "completed"}
            or completed != (self.impact_json is not None)
            or completed != (self.risk_json is not None)
            or completed != (self.risk_level is not None)
            or completed != (self.affected_count is not None)
            or completed != (self.completed_at is not None)
            or self.risk_level
            not in ({"low", "medium", "high", "critical"} if completed else {None})
            or (self.affected_count is not None and self.affected_count < 0)
            or self.planned_at.tzinfo is None
            or (self.completed_at is not None and self.completed_at < self.planned_at)
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid()
        if completed:
            if self.impact_json is None or self.risk_json is None:
                _invalid()
            if (
                len(self.impact_json.encode()) > _MAX_EVIDENCE_BYTES
                or len(self.risk_json.encode()) > _MAX_EVIDENCE_BYTES
            ):
                _invalid()
            impact, risk = _parse_impact(self.impact_json), _parse_risk(self.risk_json)
            if (
                _serialize(impact) != self.impact_json
                or _serialize(risk) != self.risk_json
                or risk.level != self.risk_level
                or len(risk.affected_entities) != self.affected_count
                or (
                    dependencies is not None
                    and runtime is not None
                    and (
                        self.impact(dependencies, runtime) != impact
                        or self.risk(dependencies, runtime) != risk
                    )
                )
            ):
                _invalid()
        if self.record_sha256 != _digest(self.values_without_digest()):
            _invalid()


def _serialize(value: CandidateImpactAnalysis | CandidateRiskClassification) -> str:
    return json.dumps(asdict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _parse_impact(raw: str) -> CandidateImpactAnalysis:
    try:
        data = json.loads(raw)
        entities = tuple(
            CandidateEntityImpact(
                **{
                    **item,
                    **{
                        key: tuple(item[key])
                        for key in (
                            "device_ids",
                            "integration_ids",
                            "area_ids",
                            "floor_ids",
                            "label_ids",
                            "derived_relations",
                            "unresolved",
                        )
                    },
                }
            )
            for item in data.pop("entities")
        )
        return CandidateImpactAnalysis(**data, entities=entities)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _invalid()


def _parse_risk(raw: str) -> CandidateRiskClassification:
    try:
        data = json.loads(raw)
        for key in (
            "reasons",
            "changed_paths",
            "affected_entities",
            "unresolved_references",
            "dynamic_paths",
            "unanalyzed_paths",
        ):
            data[key] = tuple(data[key])
        return CandidateRiskClassification(**data)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _invalid()


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(UTC)


def _digest(values: tuple[object, ...]) -> str:
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _text(value: object) -> str:
    if type(value) is not str:
        _invalid()
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        _invalid()
    return value


def _invalid() -> NoReturn:
    raise CandidateRiskCheckpointError("candidate risk checkpoint is invalid")
