from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from ha_syncapp.candidate_dependencies import (
    CandidateDependencyAnalysis,
    CandidateDependencyFile,
)
from ha_syncapp.candidate_impact import CandidateImpactError, expand_candidate_impact
from ha_syncapp.runtime_evidence import fingerprint_runtime
from ha_syncapp.runtime_inventory import RuntimeInventoryInput

TARGET = "Owner/Home"
REPOSITORY_ID = 42
BASELINE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
MANIFEST_SHA = "c" * 64


def _runtime() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={"home_assistant_version": "2026.9.1"},
        homeassistant={
            "entities": [
                {
                    "entity_id": "automation.arrival",
                    "device_id": None,
                    "config_entry_id": None,
                    "labels": [],
                },
                {
                    "entity_id": "light.kitchen",
                    "device_id": "device-1",
                    "config_entry_id": "entry-1",
                    "labels": ["label-entity"],
                },
                {
                    "entity_id": "scene.evening",
                    "device_id": None,
                    "config_entry_id": None,
                    "labels": [],
                },
                {
                    "entity_id": "script.notify_family",
                    "device_id": None,
                    "config_entry_id": None,
                    "labels": [],
                },
            ],
            "devices": [
                {
                    "id": "device-1",
                    "area_id": "area-1",
                    "config_entries": ["entry-1"],
                    "labels": ["label-device"],
                }
            ],
            "integrations": [{"entry_id": "entry-1", "domain": "hue"}],
            "areas": [
                {
                    "id": "area-1",
                    "floor_id": "floor-1",
                    "labels": ["label-area"],
                }
            ],
            "floors": [{"id": "floor-1"}],
            "labels": [
                {"id": "label-area"},
                {"id": "label-device"},
                {"id": "label-entity"},
            ],
            "states": [
                {"entity_id": "automation.arrival", "state": "on"},
                {"entity_id": "light.kitchen", "state": "on"},
                {"entity_id": "scene.evening", "state": "scening"},
                {"entity_id": "script.notify_family", "state": "off"},
            ],
            "services": [{"domain": "light"}],
        },
    )


def _dependencies(runtime: RuntimeInventoryInput) -> CandidateDependencyAnalysis:
    return CandidateDependencyAnalysis(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        stage_manifest_sha256=MANIFEST_SHA,
        runtime_sha256=fingerprint_runtime(runtime),
        analysis_method="best_effort_lexical",
        files=(
            CandidateDependencyFile(
                path="automations.yaml",
                disposition="analyzed_text",
                known_entity_references=(
                    "automation.arrival",
                    "light.kitchen",
                    "scene.evening",
                    "script.notify_family",
                ),
                unknown_object_references=(),
                dynamic_reference=False,
            ),
        ),
        known_entity_references=(
            "automation.arrival",
            "light.kitchen",
            "scene.evening",
            "script.notify_family",
        ),
        unknown_object_references=(),
        dynamic_paths=(),
        unanalyzed_paths=(),
    )


def test_expands_entity_references_into_deterministic_runtime_impact() -> None:
    runtime = _runtime()

    result = expand_candidate_impact(_dependencies(runtime), runtime)

    assert result.target == TARGET
    assert result.repository_id == REPOSITORY_ID
    assert result.baseline_sha == BASELINE_SHA
    assert result.candidate_sha == CANDIDATE_SHA
    assert result.stage_manifest_sha256 == MANIFEST_SHA
    assert result.runtime_sha256 == fingerprint_runtime(runtime)
    assert tuple(item.entity_id for item in result.entities) == (
        "automation.arrival",
        "light.kitchen",
        "scene.evening",
        "script.notify_family",
    )

    light = next(item for item in result.entities if item.entity_id == "light.kitchen")
    assert light.domain == "light"
    assert light.object_kind == "entity"
    assert light.device_ids == ("device-1",)
    assert light.integration_ids == ("entry-1",)
    assert light.area_ids == ("area-1",)
    assert light.floor_ids == ("floor-1",)
    assert light.label_ids == ("label-area", "label-device", "label-entity")
    assert light.derived_relations == ("area-1",)
    assert light.unresolved == ()

    kinds = {item.entity_id: item.object_kind for item in result.entities}
    assert kinds == {
        "automation.arrival": "automation",
        "light.kitchen": "entity",
        "scene.evening": "scene",
        "script.notify_family": "script",
    }


def test_runtime_fingerprint_is_order_independent_for_mapping_keys() -> None:
    first = RuntimeInventoryInput(
        manifest={"b": 2, "a": 1},
        homeassistant={"entities": [{"entity_id": "light.one", "name": "One"}]},
    )
    second = RuntimeInventoryInput(
        manifest={"a": 1, "b": 2},
        homeassistant={"entities": [{"name": "One", "entity_id": "light.one"}]},
    )

    assert fingerprint_runtime(first) == fingerprint_runtime(second)


def test_rejects_runtime_snapshot_mismatch() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime)
    changed = RuntimeInventoryInput(
        manifest=runtime.manifest,
        homeassistant={**runtime.homeassistant, "services": [{"domain": "switch"}]},
    )

    with pytest.raises(CandidateImpactError, match="runtime binding"):
        expand_candidate_impact(dependencies, changed)


def test_rejects_tampered_dependency_summary() -> None:
    runtime = _runtime()
    dependencies = replace(
        _dependencies(runtime),
        known_entity_references=("light.kitchen",),
    )

    with pytest.raises(CandidateImpactError, match="dependency evidence"):
        expand_candidate_impact(dependencies, runtime)


def test_preserves_unresolved_registry_links_in_impact() -> None:
    runtime = _runtime()
    homeassistant = dict(runtime.homeassistant)
    entities = [
        dict(item)
        for item in cast(list[dict[str, object]], homeassistant["entities"])
    ]
    light = next(item for item in entities if item["entity_id"] == "light.kitchen")
    light["config_entry_id"] = "entry-missing"
    homeassistant["entities"] = entities
    changed_runtime = RuntimeInventoryInput(
        manifest=runtime.manifest,
        homeassistant=homeassistant,
    )
    dependencies = _dependencies(changed_runtime)

    result = expand_candidate_impact(dependencies, changed_runtime)

    light_impact = next(item for item in result.entities if item.entity_id == "light.kitchen")
    assert light_impact.integration_ids == ("entry-1",)
    assert light_impact.unresolved == ("integration:entry-missing",)


def test_malformed_runtime_topology_fails_closed() -> None:
    runtime = _runtime()
    malformed = RuntimeInventoryInput(
        manifest=runtime.manifest,
        homeassistant={
            **runtime.homeassistant,
            "devices": [{"id": "device-1"}, {"id": "device-1"}],
        },
    )
    dependencies = replace(
        _dependencies(runtime),
        runtime_sha256=fingerprint_runtime(malformed),
    )

    with pytest.raises(CandidateImpactError, match="runtime topology"):
        expand_candidate_impact(dependencies, malformed)
