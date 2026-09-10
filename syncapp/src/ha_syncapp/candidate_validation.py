"""Fail-closed static validation for an integrity-bound isolated candidate Stage."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode

from . import candidate_stage as stage_module
from .candidate_dependencies import CandidateDependencyAnalysis
from .candidate_impact import CandidateImpactAnalysis
from .candidate_integrity import CandidateIntegrity
from .candidate_risk import (
    CandidateRiskClassification,
    CandidateRiskError,
    verify_candidate_risk_classification,
)
from .candidate_stage import CandidateStage, CandidateStageEntry, CandidateStageError
from .runtime_inventory import RuntimeInventoryInput

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CONFLICT_MARKER = re.compile(r"^(?:<<<<<<<|=======|>>>>>>>)", re.MULTILINE)
_MAX_STATIC_PARSE_BYTES = 4 * 1024 * 1024
_HA_SCALAR_TAGS = (
    "!include",
    "!include_dir_list",
    "!include_dir_named",
    "!include_dir_merge_list",
    "!include_dir_merge_named",
    "!secret",
)
_ALLOWED_STATUSES = {"valid", "invalid", "unvalidated", "deleted"}
_ALLOWED_FORMATS = {"yaml", "json", "text", "binary", "deleted"}


class CandidateValidationError(RuntimeError):
    """Static candidate validation evidence could not be established safely."""


@dataclass(frozen=True, slots=True)
class CandidateValidationFile:
    """Deterministic static-validation evidence for one changed candidate path."""

    path: str
    status: str
    format: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateStaticValidation:
    """Immutable static-validation result bound to exact candidate/runtime/risk evidence."""

    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    runtime_sha256: str
    risk_level: str
    files: tuple[CandidateValidationFile, ...]
    invalid_paths: tuple[str, ...]
    unvalidated_paths: tuple[str, ...]
    syntax_valid: bool
    semantic_home_assistant_validation_required: bool = True


class _CandidateYamlLoader(yaml.SafeLoader):
    """Safe YAML loader with only Home Assistant scalar indirection tags added."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise yaml.constructor.ConstructorError(None, None, "mapping expected", node.start_mark)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found a duplicate key",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _construct_ha_scalar(loader: _CandidateYamlLoader, node: Node) -> str:
    if not isinstance(node, ScalarNode):
        raise yaml.constructor.ConstructorError(
            None,
            None,
            "Home Assistant indirection tag requires a scalar",
            node.start_mark,
        )
    return loader.construct_scalar(node)


for _tag in _HA_SCALAR_TAGS:
    _CandidateYamlLoader.add_constructor(_tag, _construct_ha_scalar)


def validate_candidate_configuration(
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    risk: CandidateRiskClassification,
    runtime: RuntimeInventoryInput,
) -> CandidateStaticValidation:
    """Validate staged syntax/structure without executing or applying candidate content."""
    _validate_bindings(integrity, stage, dependencies, impact, risk, runtime)
    try:
        stage_module.verify_candidate_stage(stage)
        entries = {entry.path: entry for entry in stage.entries}
        if len(entries) != len(stage.entries):
            raise CandidateValidationError("candidate staged path evidence is invalid")
        files = tuple(
            _validate_path(path, entries.get(path), stage) for path in integrity.changed_paths
        )
        stage_module.verify_candidate_stage(stage)
    except CandidateValidationError:
        raise
    except CandidateStageError as exc:
        raise CandidateValidationError(
            "candidate static validation evidence could not be established"
        ) from exc

    invalid_paths = tuple(item.path for item in files if item.status == "invalid")
    unvalidated_paths = tuple(item.path for item in files if item.status == "unvalidated")
    result = CandidateStaticValidation(
        target=integrity.target,
        repository_id=integrity.repository_id,
        baseline_sha=integrity.baseline_sha,
        candidate_sha=integrity.candidate_sha,
        stage_manifest_sha256=integrity.stage_manifest_sha256,
        runtime_sha256=risk.runtime_sha256,
        risk_level=risk.level,
        files=files,
        invalid_paths=invalid_paths,
        unvalidated_paths=unvalidated_paths,
        syntax_valid=not invalid_paths,
    )
    _validate_result(result)
    return result


def verify_candidate_static_validation(
    result: CandidateStaticValidation,
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    risk: CandidateRiskClassification,
    runtime: RuntimeInventoryInput,
) -> None:
    """Recompute static validation and require exact evidence equality."""
    _validate_result(result)
    expected = validate_candidate_configuration(
        integrity,
        stage,
        dependencies,
        impact,
        risk,
        runtime,
    )
    if result != expected:
        raise CandidateValidationError("candidate static validation evidence does not match inputs")


def _validate_bindings(
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    risk: CandidateRiskClassification,
    runtime: RuntimeInventoryInput,
) -> None:
    if (
        type(integrity) is not CandidateIntegrity
        or type(stage) is not CandidateStage
        or type(dependencies) is not CandidateDependencyAnalysis
        or type(impact) is not CandidateImpactAnalysis
        or type(risk) is not CandidateRiskClassification
        or type(runtime) is not RuntimeInventoryInput
    ):
        raise CandidateValidationError("candidate static validation inputs are invalid")
    try:
        verify_candidate_risk_classification(risk, dependencies, impact, runtime)
    except CandidateRiskError as exc:
        raise CandidateValidationError("candidate risk evidence is not trusted") from exc

    if (
        integrity.target != stage.target
        or integrity.repository_id != stage.repository_id
        or integrity.candidate_sha != stage.commit_sha
        or integrity.stage_manifest_sha256 != stage.manifest_sha256
        or stage.branch != "candidate"
    ):
        raise CandidateValidationError("candidate static validation Stage bindings do not match")

    for evidence in (dependencies, impact, risk):
        if (
            evidence.target != integrity.target
            or evidence.repository_id != integrity.repository_id
            or evidence.baseline_sha != integrity.baseline_sha
            or evidence.candidate_sha != integrity.candidate_sha
            or evidence.stage_manifest_sha256 != integrity.stage_manifest_sha256
            or evidence.runtime_sha256 != risk.runtime_sha256
        ):
            raise CandidateValidationError("candidate static validation evidence bindings do not match")
    changed_paths = tuple(item.path for item in dependencies.files)
    if changed_paths != integrity.changed_paths or risk.changed_paths != integrity.changed_paths:
        raise CandidateValidationError("candidate static validation changed paths do not match")


def _validate_path(
    path: str,
    entry: CandidateStageEntry | None,
    stage: CandidateStage,
) -> CandidateValidationFile:
    if entry is None:
        return CandidateValidationFile(path=path, status="deleted", format="deleted")
    if entry.path != path:
        raise CandidateValidationError("candidate staged path evidence is invalid")
    if entry.size > _MAX_STATIC_PARSE_BYTES:
        return CandidateValidationFile(
            path=path,
            status="unvalidated",
            format=_format_for_path(path),
            reasons=("static_parse_size_limit",),
        )

    data = _read_bound_bytes(stage, entry)
    if b"\x00" in data:
        return CandidateValidationFile(
            path=path,
            status="unvalidated",
            format="binary",
            reasons=("binary_or_non_utf8",),
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return CandidateValidationFile(
            path=path,
            status="unvalidated",
            format="binary",
            reasons=("binary_or_non_utf8",),
        )

    file_format = _format_for_path(path)
    if _CONFLICT_MARKER.search(text):
        return CandidateValidationFile(
            path=path,
            status="invalid",
            format=file_format,
            reasons=("merge_conflict_marker",),
        )
    if file_format == "yaml":
        return _validate_yaml(path, text)
    if file_format == "json":
        return _validate_json(path, text)
    return CandidateValidationFile(
        path=path,
        status="unvalidated",
        format="text",
        reasons=("unsupported_static_format",),
    )


def _read_bound_bytes(stage: CandidateStage, entry: CandidateStageEntry) -> bytes:
    expected_mode = 0o700 if entry.git_mode == "100755" else 0o600
    try:
        data = stage_module._read_private_file(
            stage.tree.joinpath(*entry.path.split("/")), expected_mode=expected_mode
        )
    except CandidateStageError as exc:
        raise CandidateValidationError("candidate staged bytes could not be read safely") from exc
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise CandidateValidationError("candidate staged bytes do not match integrity evidence")
    return data


def _format_for_path(path: str) -> str:
    pure = PurePosixPath(path)
    suffix = pure.suffix.casefold()
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    if suffix == ".json" or (pure.parts and pure.parts[0] == ".storage"):
        return "json"
    return "text"


def _validate_yaml(path: str, text: str) -> CandidateValidationFile:
    try:
        documents = list(yaml.load_all(text, Loader=_CandidateYamlLoader))
    except (yaml.YAMLError, RecursionError, ValueError):
        return CandidateValidationFile(
            path=path,
            status="invalid",
            format="yaml",
            reasons=("yaml_syntax_or_tag",),
        )
    if len(documents) > 1:
        return CandidateValidationFile(
            path=path,
            status="invalid",
            format="yaml",
            reasons=("yaml_multiple_documents",),
        )
    return CandidateValidationFile(path=path, status="valid", format="yaml")


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _json_object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _validate_json(path: str, text: str) -> CandidateValidationFile:
    try:
        json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_json_object_no_duplicates,
        )
    except (json.JSONDecodeError, RecursionError, ValueError):
        return CandidateValidationFile(
            path=path,
            status="invalid",
            format="json",
            reasons=("json_syntax_or_structure",),
        )
    return CandidateValidationFile(path=path, status="valid", format="json")


def _validate_result(result: CandidateStaticValidation) -> None:
    if (
        type(result) is not CandidateStaticValidation
        or not result.target
        or result.repository_id <= 0
        or _OBJECT_ID.fullmatch(result.baseline_sha) is None
        or _OBJECT_ID.fullmatch(result.candidate_sha) is None
        or _DIGEST.fullmatch(result.stage_manifest_sha256) is None
        or _DIGEST.fullmatch(result.runtime_sha256) is None
        or result.risk_level not in {"low", "medium", "high", "critical"}
        or type(result.syntax_valid) is not bool
        or result.semantic_home_assistant_validation_required is not True
    ):
        raise CandidateValidationError("candidate static validation result is invalid")

    paths = tuple(item.path for item in result.files)
    if tuple(sorted(set(paths))) != paths:
        raise CandidateValidationError("candidate static validation result is invalid")
    for item in result.files:
        if (
            type(item) is not CandidateValidationFile
            or not item.path
            or item.status not in _ALLOWED_STATUSES
            or item.format not in _ALLOWED_FORMATS
            or tuple(sorted(set(item.reasons))) != item.reasons
            or any(not reason for reason in item.reasons)
            or (item.status in {"valid", "deleted"} and item.reasons)
            or (item.status in {"invalid", "unvalidated"} and not item.reasons)
        ):
            raise CandidateValidationError("candidate static validation file result is invalid")

    invalid_paths = tuple(item.path for item in result.files if item.status == "invalid")
    unvalidated_paths = tuple(item.path for item in result.files if item.status == "unvalidated")
    if (
        result.invalid_paths != invalid_paths
        or result.unvalidated_paths != unvalidated_paths
        or result.syntax_valid is not (not invalid_paths)
    ):
        raise CandidateValidationError("candidate static validation result is invalid")
