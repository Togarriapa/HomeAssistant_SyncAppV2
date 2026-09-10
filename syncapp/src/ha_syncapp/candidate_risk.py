"""Conservative deployment risk classification for integrity-bound candidate evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .candidate_dependencies import CandidateDependencyAnalysis
from .candidate_impact import (
    CandidateImpactAnalysis,
    CandidateImpactError,
    verify_candidate_impact_analysis,
)
from .runtime_inventory import RuntimeInventoryInput

_LEVELS = ("low", "medium", "high", "critical")
_LEVEL_INDEX = {level: index for index, level in enumerate(_LEVELS)}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_DATABASE_SIDECAR_SUFFIXES = (".db-wal", ".db-shm", ".db-journal")


class CandidateRiskError(RuntimeError):
    """Candidate risk could not be classified from trusted evidence."""


@dataclass(frozen=True, slots=True)
class CandidateRiskClassification:
    """Immutable deployment-risk evidence for one exact candidate/runtime pair."""

    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    runtime_sha256: str
    level: str
    reasons: tuple[str, ...]
    changed_paths: tuple[str, ...]
    affected_entities: tuple[str, ...]
    unresolved_references: tuple[str, ...]
    dynamic_paths: tuple[str, ...]
    unanalyzed_paths: tuple[str, ...]


def classify_candidate_risk(
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    runtime: RuntimeInventoryInput,
) -> CandidateRiskClassification:
    """Assign the README risk level without granting validation or deployment authority."""
    try:
        verify_candidate_impact_analysis(impact, dependencies, runtime)
    except CandidateImpactError as exc:
        raise CandidateRiskError("candidate impact evidence is not trusted") from exc

    changed_paths = tuple(item.path for item in dependencies.files)
    if not changed_paths:
        raise CandidateRiskError("candidate risk requires at least one changed path")

    level = "low"
    reasons: set[str] = set()
    for path in changed_paths:
        path_level, basis = _classify_path(path)
        level = _maximum_level(level, path_level)
        reasons.add(f"{path_level} path {path}: {basis}")

    unresolved = set(dependencies.unknown_object_references)
    for entity in impact.entities:
        unresolved.update(entity.unresolved)

    if dependencies.dynamic_paths:
        level = _maximum_level(level, "high")
        reasons.add("dynamic candidate references require conservative handling")
    if dependencies.unanalyzed_paths:
        level = _maximum_level(level, "high")
        reasons.add("unanalyzed candidate bytes require conservative handling")
    if dependencies.unknown_object_references:
        level = _maximum_level(level, "high")
        reasons.add("unknown candidate object references require conservative handling")
    if any(entity.unresolved for entity in impact.entities):
        level = _maximum_level(level, "high")
        reasons.add("unresolved runtime relationships require conservative handling")

    result = CandidateRiskClassification(
        target=dependencies.target,
        repository_id=dependencies.repository_id,
        baseline_sha=dependencies.baseline_sha,
        candidate_sha=dependencies.candidate_sha,
        stage_manifest_sha256=dependencies.stage_manifest_sha256,
        runtime_sha256=dependencies.runtime_sha256,
        level=level,
        reasons=tuple(sorted(reasons)),
        changed_paths=changed_paths,
        affected_entities=tuple(item.entity_id for item in impact.entities),
        unresolved_references=tuple(sorted(unresolved)),
        dynamic_paths=dependencies.dynamic_paths,
        unanalyzed_paths=dependencies.unanalyzed_paths,
    )
    _validate_result(result)
    return result


def verify_candidate_risk_classification(
    result: CandidateRiskClassification,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    runtime: RuntimeInventoryInput,
) -> None:
    """Recompute risk from trusted evidence and require an exact deterministic result."""
    _validate_result(result)
    expected = classify_candidate_risk(dependencies, impact, runtime)
    if result != expected:
        raise CandidateRiskError("candidate risk evidence does not match verified inputs")


def _classify_path(path: str) -> tuple[str, str]:
    pure = PurePosixPath(path)
    parts = tuple(part.casefold() for part in pure.parts)
    if (
        not path
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or pure.as_posix() != path
    ):
        raise CandidateRiskError("candidate risk path is invalid")

    basename = parts[-1]
    if basename.endswith(_DATABASE_SUFFIXES) or basename.endswith(_DATABASE_SIDECAR_SUFFIXES):
        return "critical", "database manipulation"

    if parts[0] == ".storage":
        return "high", "Home Assistant internal storage"
    if parts[0] == "custom_components":
        return "high", "custom component"
    if basename == "configuration.yaml":
        return "high", "core Home Assistant configuration"
    if basename == "secrets.yaml" or "secret" in basename:
        return "high", "sensitive secret configuration"
    if basename.startswith("auth") or "authentication" in basename:
        return "high", "authentication-related configuration"

    if parts[0] in {"dashboards", "packages"} or basename in {
        "ui-lovelace.yaml",
        "lovelace.yaml",
    }:
        return "medium", "multi-resource dashboard or package configuration"

    if basename in {"automations.yaml", "scripts.yaml", "scenes.yaml"}:
        return "low", "automation, script, or scene configuration"
    if parts[0] in {"automations", "scripts", "scenes", "blueprints"}:
        return "low", "automation, script, scene, or blueprint configuration"
    if pure.suffix.casefold() in {".yaml", ".yml"}:
        return "low", "non-critical YAML by path"

    return "high", "unrecognized candidate path requires conservative handling"


def _maximum_level(first: str, second: str) -> str:
    return first if _LEVEL_INDEX[first] >= _LEVEL_INDEX[second] else second


def _validate_result(result: CandidateRiskClassification) -> None:
    if (
        type(result) is not CandidateRiskClassification
        or not result.target
        or result.repository_id <= 0
        or _OBJECT_ID.fullmatch(result.baseline_sha) is None
        or _OBJECT_ID.fullmatch(result.candidate_sha) is None
        or _DIGEST.fullmatch(result.stage_manifest_sha256) is None
        or _DIGEST.fullmatch(result.runtime_sha256) is None
        or result.level not in _LEVELS
        or not result.reasons
        or not result.changed_paths
    ):
        raise CandidateRiskError("candidate risk result is invalid")

    for values in (
        result.reasons,
        result.changed_paths,
        result.affected_entities,
        result.unresolved_references,
        result.dynamic_paths,
        result.unanalyzed_paths,
    ):
        if tuple(sorted(set(values))) != values or any(not value for value in values):
            raise CandidateRiskError("candidate risk result is invalid")

    if not set(result.dynamic_paths).issubset(result.changed_paths):
        raise CandidateRiskError("candidate risk result is invalid")
    if not set(result.unanalyzed_paths).issubset(result.changed_paths):
        raise CandidateRiskError("candidate risk result is invalid")
