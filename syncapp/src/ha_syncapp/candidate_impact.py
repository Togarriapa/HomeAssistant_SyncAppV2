"""Expand conservative candidate references through trusted runtime topology evidence."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import cast

from .candidate_dependencies import (
    CandidateDependencyAnalysis,
    CandidateDependencyError,
    verify_candidate_dependency_analysis,
)
from .runtime_inventory import RuntimeInventoryInput
from .runtime_topology import RuntimeTopologyError, build_runtime_topology

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SPECIAL_ENTITY_KINDS = {"automation", "script", "scene"}


class CandidateImpactError(RuntimeError):
    """Candidate runtime impact evidence could not be established safely."""


@dataclass(frozen=True, slots=True)
class CandidateEntityImpact:
    """Runtime relationships reachable from one known candidate entity reference."""

    entity_id: str
    domain: str
    object_kind: str
    device_ids: tuple[str, ...]
    integration_ids: tuple[str, ...]
    area_ids: tuple[str, ...]
    floor_ids: tuple[str, ...]
    label_ids: tuple[str, ...]
    derived_relations: tuple[str, ...]
    unresolved: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateImpactAnalysis:
    """Immutable runtime impact evidence for one dependency-analyzed candidate."""

    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    runtime_sha256: str
    entities: tuple[CandidateEntityImpact, ...]


def expand_candidate_impact(
    dependencies: CandidateDependencyAnalysis,
    runtime: RuntimeInventoryInput,
) -> CandidateImpactAnalysis:
    """Expand known entity references through the exact bound runtime snapshot."""
    try:
        verify_candidate_dependency_analysis(dependencies, runtime)
        topology_evidence = build_runtime_topology(runtime)
    except CandidateDependencyError as exc:
        message = "candidate dependency evidence is invalid"
        if "runtime binding" in str(exc):
            message = "candidate dependency runtime binding does not match"
        raise CandidateImpactError(message) from exc
    except RuntimeTopologyError as exc:
        raise CandidateImpactError("candidate impact runtime topology is invalid") from exc

    topology = topology_evidence.analysis.get("topology")
    if not isinstance(topology, dict):
        raise CandidateImpactError("candidate impact runtime topology is invalid")
    edges_raw = topology.get("edges")
    unresolved_raw = topology.get("unresolved")
    if not isinstance(edges_raw, list) or not isinstance(unresolved_raw, list):
        raise CandidateImpactError("candidate impact runtime topology is invalid")

    edges = _validated_edges(edges_raw, allow_authority=True)
    unresolved_edges = _validated_edges(unresolved_raw, allow_authority=False)
    outgoing = _group_edges(edges)
    unresolved = _group_edges(unresolved_edges)

    entity_impacts = tuple(
        _expand_entity(entity_id, outgoing, unresolved)
        for entity_id in dependencies.known_entity_references
    )
    result = CandidateImpactAnalysis(
        target=dependencies.target,
        repository_id=dependencies.repository_id,
        baseline_sha=dependencies.baseline_sha,
        candidate_sha=dependencies.candidate_sha,
        stage_manifest_sha256=dependencies.stage_manifest_sha256,
        runtime_sha256=dependencies.runtime_sha256,
        entities=entity_impacts,
    )
    _validate_result(result)
    return result


def _validated_edges(
    raw: list[object],
    *,
    allow_authority: bool,
) -> tuple[dict[str, str], ...]:
    edges: list[dict[str, str]] = []
    expected_keys = {"source_id", "source_type", "relation", "target_id", "target_type"}
    if allow_authority:
        expected_keys.add("authority")
    for item in raw:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise CandidateImpactError("candidate impact runtime topology is invalid")
        if any(not isinstance(value, str) or not value for value in item.values()):
            raise CandidateImpactError("candidate impact runtime topology is invalid")
        edge = cast(dict[str, str], dict(item))
        if allow_authority and edge["authority"] not in {"registry", "derived"}:
            raise CandidateImpactError("candidate impact runtime topology is invalid")
        edges.append(edge)
    key = lambda edge: (
        edge["source_type"],
        edge["source_id"],
        edge["relation"],
        edge["target_type"],
        edge["target_id"],
    )
    if edges != sorted(edges, key=key) or len({key(edge) for edge in edges}) != len(edges):
        raise CandidateImpactError("candidate impact runtime topology is invalid")
    return tuple(edges)


def _group_edges(
    edges: tuple[dict[str, str], ...],
) -> dict[tuple[str, str], tuple[dict[str, str], ...]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for edge in edges:
        grouped[(edge["source_type"], edge["source_id"])].append(edge)
    return {key: tuple(value) for key, value in sorted(grouped.items())}


def _expand_entity(
    entity_id: str,
    outgoing: dict[tuple[str, str], tuple[dict[str, str], ...]],
    unresolved: dict[tuple[str, str], tuple[dict[str, str], ...]],
) -> CandidateEntityImpact:
    if "." not in entity_id:
        raise CandidateImpactError("candidate dependency entity identifier is invalid")
    domain, _ = entity_id.split(".", 1)
    object_kind = domain if domain in _SPECIAL_ENTITY_KINDS else "entity"

    devices: set[str] = set()
    integrations: set[str] = set()
    areas: set[str] = set()
    floors: set[str] = set()
    labels: set[str] = set()
    derived_relations: set[str] = set()
    unresolved_items: set[str] = set()

    entity_edges = outgoing.get(("entity", entity_id), ())
    _collect(entity_edges, devices, integrations, areas, labels, derived_relations)
    _collect_unresolved(unresolved.get(("entity", entity_id), ()), unresolved_items)

    for device_id in sorted(devices):
        device_edges = outgoing.get(("device", device_id), ())
        _collect(device_edges, devices, integrations, areas, labels, derived_relations)
        _collect_unresolved(unresolved.get(("device", device_id), ()), unresolved_items)

    for area_id in sorted(areas):
        area_edges = outgoing.get(("area", area_id), ())
        for edge in area_edges:
            if edge["relation"] == "on_floor" and edge["target_type"] == "floor":
                floors.add(edge["target_id"])
            elif edge["relation"] == "has_label" and edge["target_type"] == "label":
                labels.add(edge["target_id"])
        _collect_unresolved(unresolved.get(("area", area_id), ()), unresolved_items)

    return CandidateEntityImpact(
        entity_id=entity_id,
        domain=domain,
        object_kind=object_kind,
        device_ids=tuple(sorted(devices)),
        integration_ids=tuple(sorted(integrations)),
        area_ids=tuple(sorted(areas)),
        floor_ids=tuple(sorted(floors)),
        label_ids=tuple(sorted(labels)),
        derived_relations=tuple(sorted(derived_relations)),
        unresolved=tuple(sorted(unresolved_items)),
    )


def _collect(
    edges: tuple[dict[str, str], ...],
    devices: set[str],
    integrations: set[str],
    areas: set[str],
    labels: set[str],
    derived_relations: set[str],
) -> None:
    for edge in edges:
        relation = edge["relation"]
        target_type = edge["target_type"]
        target_id = edge["target_id"]
        if relation == "belongs_to_device" and target_type == "device":
            devices.add(target_id)
        elif relation == "configured_by" and target_type == "integration":
            integrations.add(target_id)
        elif relation == "located_in" and target_type == "area":
            areas.add(target_id)
            if edge.get("authority") == "derived":
                derived_relations.add(target_id)
        elif relation == "has_label" and target_type == "label":
            labels.add(target_id)


def _collect_unresolved(
    edges: tuple[dict[str, str], ...],
    output: set[str],
) -> None:
    for edge in edges:
        output.add(f"{edge['target_type']}:{edge['target_id']}")


def _validate_result(result: CandidateImpactAnalysis) -> None:
    if (
        type(result) is not CandidateImpactAnalysis
        or result.repository_id <= 0
        or _DIGEST.fullmatch(result.stage_manifest_sha256) is None
        or _DIGEST.fullmatch(result.runtime_sha256) is None
        or tuple(sorted(item.entity_id for item in result.entities))
        != tuple(item.entity_id for item in result.entities)
        or len({item.entity_id for item in result.entities}) != len(result.entities)
    ):
        raise CandidateImpactError("candidate impact result is invalid")
