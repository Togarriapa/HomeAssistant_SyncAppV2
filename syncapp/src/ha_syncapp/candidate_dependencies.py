"""Conservative candidate dependency evidence bound to integrity-validated staging."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from . import candidate_stage as stage_module
from .candidate_integrity import CandidateIntegrity
from .candidate_stage import CandidateStage, CandidateStageEntry, CandidateStageError
from .runtime_inventory import RuntimeInventoryInput

_OBJECT_REFERENCE = re.compile(r"(?<![A-Za-z0-9_])([a-z0-9_]+\.[a-z0-9_]+)(?![A-Za-z0-9_])")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DYNAMIC_MARKERS = ("{{", "{%", "{#", "!include", "!secret")
_MAX_ANALYSIS_BYTES = 1024 * 1024
_ANALYSIS_METHOD = "best_effort_lexical"
_ALLOWED_DISPOSITIONS = {
    "analyzed_text",
    "deleted",
    "non_utf8_or_binary",
    "oversize",
}


class CandidateDependencyError(RuntimeError):
    """Candidate dependency evidence could not be established safely."""


@dataclass(frozen=True, slots=True)
class CandidateDependencyFile:
    """Conservative static dependency evidence for one changed candidate path."""

    path: str
    disposition: str
    known_entity_references: tuple[str, ...]
    unknown_object_references: tuple[str, ...]
    dynamic_reference: bool


@dataclass(frozen=True, slots=True)
class CandidateDependencyAnalysis:
    """Immutable dependency evidence bound to one validated candidate staging tree."""

    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    analysis_method: str
    files: tuple[CandidateDependencyFile, ...]
    known_entity_references: tuple[str, ...]
    unknown_object_references: tuple[str, ...]
    dynamic_paths: tuple[str, ...]
    unanalyzed_paths: tuple[str, ...]


def analyze_candidate_dependencies(
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    runtime: RuntimeInventoryInput,
) -> CandidateDependencyAnalysis:
    """Analyze changed staged text conservatively without executing candidate content."""
    _validate_inputs(integrity, stage, runtime)
    try:
        stage_module.verify_candidate_stage(stage)
        known_entities = _runtime_entities(runtime)
        entries = {entry.path: entry for entry in stage.entries}
        if len(entries) != len(stage.entries):
            raise CandidateDependencyError("candidate staged path evidence is invalid")
        files = tuple(
            _analyze_path(path, entries.get(path), stage, known_entities)
            for path in integrity.changed_paths
        )
        _validate_files(files, integrity.changed_paths)
        stage_module.verify_candidate_stage(stage)
    except CandidateDependencyError:
        raise
    except CandidateStageError as exc:
        raise CandidateDependencyError(
            "candidate dependency evidence could not be established"
        ) from exc

    known_references = tuple(
        sorted({reference for item in files for reference in item.known_entity_references})
    )
    unknown_references = tuple(
        sorted({reference for item in files for reference in item.unknown_object_references})
    )
    dynamic_paths = tuple(item.path for item in files if item.dynamic_reference)
    unanalyzed_paths = tuple(
        item.path for item in files if item.disposition in {"non_utf8_or_binary", "oversize"}
    )
    result = CandidateDependencyAnalysis(
        target=integrity.target,
        repository_id=integrity.repository_id,
        baseline_sha=integrity.baseline_sha,
        candidate_sha=integrity.candidate_sha,
        stage_manifest_sha256=integrity.stage_manifest_sha256,
        analysis_method=_ANALYSIS_METHOD,
        files=files,
        known_entity_references=known_references,
        unknown_object_references=unknown_references,
        dynamic_paths=dynamic_paths,
        unanalyzed_paths=unanalyzed_paths,
    )
    _validate_result(result)
    return result


def _validate_inputs(
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    runtime: RuntimeInventoryInput,
) -> None:
    if (
        type(integrity) is not CandidateIntegrity
        or type(stage) is not CandidateStage
        or type(runtime) is not RuntimeInventoryInput
    ):
        raise CandidateDependencyError("candidate dependency inputs are invalid")
    if (
        integrity.target != stage.target
        or integrity.repository_id != stage.repository_id
        or integrity.candidate_sha != stage.commit_sha
        or integrity.stage_manifest_sha256 != stage.manifest_sha256
        or stage.branch != "candidate"
    ):
        raise CandidateDependencyError("candidate dependency bindings do not match")
    if (
        not integrity.target
        or integrity.repository_id <= 0
        or _OBJECT_ID.fullmatch(integrity.baseline_sha) is None
        or _OBJECT_ID.fullmatch(integrity.candidate_sha) is None
        or _DIGEST.fullmatch(integrity.stage_manifest_sha256) is None
        or tuple(sorted(set(integrity.changed_paths))) != integrity.changed_paths
    ):
        raise CandidateDependencyError("candidate dependency integrity evidence is invalid")


def _runtime_entities(runtime: RuntimeInventoryInput) -> frozenset[str]:
    raw_entities = runtime.homeassistant.get("entities", [])
    if not isinstance(raw_entities, list):
        raise CandidateDependencyError("runtime entity evidence is invalid")
    entities: set[str] = set()
    for raw in raw_entities:
        if not isinstance(raw, dict):
            raise CandidateDependencyError("runtime entity evidence is invalid")
        entity_id = raw.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id or entity_id in entities:
            raise CandidateDependencyError("runtime entity evidence is invalid")
        entities.add(entity_id)
    return frozenset(entities)


def _analyze_path(
    path: str,
    entry: CandidateStageEntry | None,
    stage: CandidateStage,
    known_entities: frozenset[str],
) -> CandidateDependencyFile:
    if entry is None:
        return CandidateDependencyFile(path, "deleted", (), (), False)
    if entry.path != path:
        raise CandidateDependencyError("candidate staged path evidence is invalid")
    if entry.size > _MAX_ANALYSIS_BYTES:
        return CandidateDependencyFile(path, "oversize", (), (), False)

    expected_mode = 0o700 if entry.git_mode == "100755" else 0o600
    try:
        data = stage_module._read_private_file(
            stage.tree.joinpath(*path.split("/")), expected_mode=expected_mode
        )
    except CandidateStageError as exc:
        raise CandidateDependencyError("candidate staged bytes could not be read safely") from exc
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise CandidateDependencyError("candidate staged bytes do not match integrity evidence")
    if b"\x00" in data:
        return CandidateDependencyFile(path, "non_utf8_or_binary", (), (), False)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return CandidateDependencyFile(path, "non_utf8_or_binary", (), (), False)

    references = set(_OBJECT_REFERENCE.findall(text))
    known = tuple(sorted(references & known_entities))
    unknown = tuple(sorted(references - known_entities))
    dynamic = any(marker in text for marker in _DYNAMIC_MARKERS)
    return CandidateDependencyFile(path, "analyzed_text", known, unknown, dynamic)


def _validate_files(
    files: tuple[CandidateDependencyFile, ...],
    changed_paths: tuple[str, ...],
) -> None:
    if tuple(item.path for item in files) != changed_paths:
        raise CandidateDependencyError("candidate dependency file evidence is invalid")
    for item in files:
        if (
            type(item) is not CandidateDependencyFile
            or item.disposition not in _ALLOWED_DISPOSITIONS
            or tuple(sorted(set(item.known_entity_references))) != item.known_entity_references
            or tuple(sorted(set(item.unknown_object_references))) != item.unknown_object_references
            or set(item.known_entity_references) & set(item.unknown_object_references)
            or type(item.dynamic_reference) is not bool
        ):
            raise CandidateDependencyError("candidate dependency file evidence is invalid")
        if item.disposition != "analyzed_text" and (
            item.known_entity_references or item.unknown_object_references or item.dynamic_reference
        ):
            raise CandidateDependencyError("candidate dependency file evidence is invalid")


def _validate_result(result: CandidateDependencyAnalysis) -> None:
    if (
        type(result) is not CandidateDependencyAnalysis
        or result.analysis_method != _ANALYSIS_METHOD
    ):
        raise CandidateDependencyError("candidate dependency result is invalid")
    if tuple(sorted(set(result.known_entity_references))) != result.known_entity_references:
        raise CandidateDependencyError("candidate dependency result is invalid")
    if tuple(sorted(set(result.unknown_object_references))) != result.unknown_object_references:
        raise CandidateDependencyError("candidate dependency result is invalid")
    if tuple(sorted(set(result.dynamic_paths))) != result.dynamic_paths:
        raise CandidateDependencyError("candidate dependency result is invalid")
    if tuple(sorted(set(result.unanalyzed_paths))) != result.unanalyzed_paths:
        raise CandidateDependencyError("candidate dependency result is invalid")
