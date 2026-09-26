"""Durable canonical evidence for one candidate dependency analysis."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_dependencies import (
    CandidateDependencyAnalysis,
    CandidateDependencyError,
    CandidateDependencyFile,
    verify_candidate_dependency_analysis,
)
from .runtime_inventory import RuntimeInventoryInput

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SCHEMA_VERSION = 1
_MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
_MAX_FILES = 4096


class CandidateDependencyCheckpointError(RuntimeError):
    """Persisted candidate dependency evidence is missing, corrupt, or rebound."""


@dataclass(frozen=True, slots=True)
class CandidateDependencyCheckpoint:
    """Integrity-protected dependency checkpoint for one exact candidate."""

    candidate_sha: str
    schema_version: int
    orchestration_sha256: str
    fetch_stage_sha256: str
    integrity_sha256: str
    target: str
    repository_id: int
    baseline_sha: str
    stage_manifest_sha256: str
    phase: str
    runtime_json: str | None
    dependencies_json: str | None
    reference_count: int | None
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
        target: str,
        repository_id: int,
        baseline_sha: str,
        stage_manifest_sha256: str,
        planned_at: datetime,
    ) -> CandidateDependencyCheckpoint:
        when = _timestamp(planned_at)
        result = cls(
            candidate_sha,
            _SCHEMA_VERSION,
            orchestration_sha256,
            fetch_stage_sha256,
            integrity_sha256,
            target,
            repository_id,
            baseline_sha,
            stage_manifest_sha256,
            "planned",
            None,
            None,
            None,
            when,
            None,
            "0" * 64,
        )
        planned = replace(result, record_sha256=_digest(result.values_without_digest()))
        planned.validate()
        return planned

    def complete(
        self,
        dependencies: CandidateDependencyAnalysis,
        runtime: RuntimeInventoryInput,
        *,
        completed_at: datetime,
    ) -> CandidateDependencyCheckpoint:
        self.validate()
        if self.phase != "planned":
            _invalid()
        try:
            verify_candidate_dependency_analysis(dependencies, runtime)
        except CandidateDependencyError:
            _invalid()
        if (
            dependencies.target != self.target
            or dependencies.repository_id != self.repository_id
            or dependencies.baseline_sha != self.baseline_sha
            or dependencies.candidate_sha != self.candidate_sha
            or dependencies.stage_manifest_sha256 != self.stage_manifest_sha256
        ):
            _invalid()
        runtime_json = _serialize_runtime(runtime)
        dependencies_json = _serialize_dependencies(dependencies)
        references = (
            len(dependencies.known_entity_references)
            + len(dependencies.known_service_references)
            + len(dependencies.unknown_object_references)
        )
        result = replace(
            self,
            phase="completed",
            runtime_json=runtime_json,
            dependencies_json=dependencies_json,
            reference_count=references,
            completed_at=_timestamp(completed_at),
            record_sha256="0" * 64,
        )
        completed = replace(result, record_sha256=_digest(result.values_without_digest()))
        completed.validate()
        return completed

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CandidateDependencyCheckpoint:
        if len(row) != 16 or type(row[6]) is not int:
            _invalid()
        try:
            planned_at = datetime.fromisoformat(_text(row[13]))
            completed_at = None if row[14] is None else datetime.fromisoformat(_text(row[14]))
        except ValueError:
            _invalid()
        result = cls(
            _text(row[0]),
            _integer(row[1]),
            _text(row[2]),
            _text(row[3]),
            _text(row[4]),
            _text(row[5]),
            _integer(row[6]),
            _text(row[7]),
            _text(row[8]),
            _text(row[9]),
            None if row[10] is None else _text(row[10]),
            None if row[11] is None else _text(row[11]),
            None if row[12] is None else _integer(row[12]),
            planned_at,
            completed_at,
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
            self.fetch_stage_sha256,
            self.integrity_sha256,
            self.target,
            self.repository_id,
            self.baseline_sha,
            self.stage_manifest_sha256,
            self.phase,
            self.runtime_json,
            self.dependencies_json,
            self.reference_count,
            self.planned_at.astimezone(UTC).isoformat(),
            None if self.completed_at is None else self.completed_at.astimezone(UTC).isoformat(),
        )

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values = self.values_without_digest()
        return (*values, _digest(values))

    def runtime(self) -> RuntimeInventoryInput:
        self.validate()
        if self.runtime_json is None:
            _invalid()
        return _parse_runtime(self.runtime_json)

    def dependencies(self) -> CandidateDependencyAnalysis:
        self.validate()
        if self.dependencies_json is None or self.runtime_json is None:
            _invalid()
        result = _parse_dependencies(self.dependencies_json)
        try:
            verify_candidate_dependency_analysis(result, _parse_runtime(self.runtime_json))
        except CandidateDependencyError:
            _invalid()
        return result

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
                    self.stage_manifest_sha256,
                )
            )
            or _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or not 0 < self.repository_id <= 2**63 - 1
            or _OBJECT_ID.fullmatch(self.baseline_sha) is None
            or self.phase not in {"planned", "completed"}
            or completed != (self.runtime_json is not None)
            or completed != (self.dependencies_json is not None)
            or completed != (self.reference_count is not None)
            or completed != (self.completed_at is not None)
            or (
                self.reference_count is not None and not 0 <= self.reference_count <= 3 * _MAX_FILES
            )
            or self.planned_at.tzinfo is None
            or self.planned_at.utcoffset() is None
            or (self.completed_at is not None and self.completed_at < self.planned_at)
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid()
        if completed:
            assert self.runtime_json is not None and self.dependencies_json is not None
            if any(
                len(value.encode("utf-8")) > _MAX_EVIDENCE_BYTES
                for value in (self.runtime_json, self.dependencies_json)
            ):
                _invalid()
            runtime = _parse_runtime(self.runtime_json)
            dependencies = _parse_dependencies(self.dependencies_json)
            try:
                verify_candidate_dependency_analysis(dependencies, runtime)
            except CandidateDependencyError:
                _invalid()
            references = (
                len(dependencies.known_entity_references)
                + len(dependencies.known_service_references)
                + len(dependencies.unknown_object_references)
            )
            if (
                _serialize_runtime(runtime) != self.runtime_json
                or _serialize_dependencies(dependencies) != self.dependencies_json
                or references != self.reference_count
                or dependencies.target != self.target
                or dependencies.repository_id != self.repository_id
                or dependencies.baseline_sha != self.baseline_sha
                or dependencies.candidate_sha != self.candidate_sha
                or dependencies.stage_manifest_sha256 != self.stage_manifest_sha256
            ):
                _invalid()
        if self.record_sha256 != _digest(self.values_without_digest()):
            _invalid()


def _serialize_runtime(runtime: RuntimeInventoryInput) -> str:
    if type(runtime) is not RuntimeInventoryInput:
        _invalid()
    return _canonical_json(
        {
            "analysis": dict(runtime.analysis),
            "deployments": dict(runtime.deployments),
            "hardware": dict(runtime.hardware),
            "homeassistant": dict(runtime.homeassistant),
            "manifest": dict(runtime.manifest),
            "supervisor": dict(runtime.supervisor),
        }
    )


def _parse_runtime(payload: str) -> RuntimeInventoryInput:
    raw = _parse_json(payload)
    expected = {"analysis", "deployments", "hardware", "homeassistant", "manifest", "supervisor"}
    if (
        not isinstance(raw, dict)
        or set(raw) != expected
        or any(not isinstance(raw[key], dict) for key in expected)
    ):
        _invalid()
    return RuntimeInventoryInput(
        manifest=raw["manifest"],
        homeassistant=raw["homeassistant"],
        supervisor=raw["supervisor"],
        hardware=raw["hardware"],
        analysis=raw["analysis"],
        deployments=raw["deployments"],
    )


def _serialize_dependencies(result: CandidateDependencyAnalysis) -> str:
    return _canonical_json(
        {
            "analysis_method": result.analysis_method,
            "baseline_sha": result.baseline_sha,
            "candidate_sha": result.candidate_sha,
            "dynamic_paths": list(result.dynamic_paths),
            "files": [
                {
                    "disposition": item.disposition,
                    "dynamic_reference": item.dynamic_reference,
                    "known_entity_references": list(item.known_entity_references),
                    "known_service_references": list(item.known_service_references),
                    "path": item.path,
                    "unknown_object_references": list(item.unknown_object_references),
                }
                for item in result.files
            ],
            "known_entity_references": list(result.known_entity_references),
            "known_service_references": list(result.known_service_references),
            "repository_id": result.repository_id,
            "runtime_sha256": result.runtime_sha256,
            "stage_manifest_sha256": result.stage_manifest_sha256,
            "target": result.target,
            "unanalyzed_paths": list(result.unanalyzed_paths),
            "unknown_object_references": list(result.unknown_object_references),
        }
    )


def _parse_dependencies(payload: str) -> CandidateDependencyAnalysis:
    raw = _parse_json(payload)
    expected = {
        "analysis_method",
        "baseline_sha",
        "candidate_sha",
        "dynamic_paths",
        "files",
        "known_entity_references",
        "known_service_references",
        "repository_id",
        "runtime_sha256",
        "stage_manifest_sha256",
        "target",
        "unanalyzed_paths",
        "unknown_object_references",
    }
    if not isinstance(raw, dict) or set(raw) != expected or not isinstance(raw["files"], list):
        _invalid()
    files: list[CandidateDependencyFile] = []
    file_keys = {
        "disposition",
        "dynamic_reference",
        "known_entity_references",
        "known_service_references",
        "path",
        "unknown_object_references",
    }
    for item in raw["files"]:
        if not isinstance(item, dict) or set(item) != file_keys:
            _invalid()
        files.append(
            CandidateDependencyFile(
                _string(item["path"]),
                _string(item["disposition"]),
                _strings(item["known_entity_references"]),
                _strings(item["unknown_object_references"]),
                _boolean(item["dynamic_reference"]),
                _strings(item["known_service_references"]),
            )
        )
    return CandidateDependencyAnalysis(
        _string(raw["target"]),
        _integer(raw["repository_id"]),
        _string(raw["baseline_sha"]),
        _string(raw["candidate_sha"]),
        _string(raw["stage_manifest_sha256"]),
        _string(raw["runtime_sha256"]),
        _string(raw["analysis_method"]),
        tuple(files),
        _strings(raw["known_entity_references"]),
        _strings(raw["unknown_object_references"]),
        _strings(raw["dynamic_paths"]),
        _strings(raw["unanalyzed_paths"]),
        _strings(raw["known_service_references"]),
    )


def _canonical_json(value: object) -> str:
    try:
        result = json.dumps(
            value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
    except (TypeError, ValueError, RecursionError):
        _invalid()
    if len(result.encode("ascii")) > _MAX_EVIDENCE_BYTES:
        _invalid()
    return result


def _parse_json(payload: str) -> object:
    try:
        return json.loads(
            payload, object_pairs_hook=_unique_object, parse_constant=lambda _: _invalid()
        )
    except (ValueError, TypeError, RecursionError):
        _invalid()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _strings(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > _MAX_FILES
        or any(not isinstance(v, str) for v in value)
    ):
        _invalid()
    return tuple(value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        _invalid()
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        _invalid()
    return value


def _text(value: object) -> str:
    return _string(value)


def _integer(value: object) -> int:
    if type(value) is not int:
        _invalid()
    return value


def _timestamp(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(UTC)


def _digest(values: tuple[object, ...]) -> str:
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _invalid() -> NoReturn:
    raise CandidateDependencyCheckpointError("candidate dependency checkpoint is invalid")
