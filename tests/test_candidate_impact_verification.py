from __future__ import annotations

from dataclasses import replace

import pytest
from ha_syncapp.candidate_dependencies import (
    CandidateDependencyAnalysis,
    CandidateDependencyFile,
)
from ha_syncapp.candidate_impact import (
    CandidateImpactError,
    expand_candidate_impact,
    verify_candidate_impact_analysis,
)
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
                    "entity_id": "light.kitchen",
                    "device_id": "device-1",
                    "config_entry_id": "entry-missing",
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
                    "labels": [],
                }
            ],
            "integrations": [{"entry_id": "entry-1", "domain": "hue"}],
            "areas": [{"id": "area-1", "floor_id": "floor-1", "labels": []}],
            "floors": [{"id": "floor-1"}],
            "labels": [],
            "states": [
                {"entity_id": "light.kitchen", "state": "on"},
                {"entity_id": "script.notify_family", "state": "off"},
            ],
            "services": [{"domain": "light"}],
        },
    )


def _dependencies(runtime: RuntimeInventoryInput) -> CandidateDependencyAnalysis:
    references = ("light.kitchen", "script.notify_family")
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
                known_entity_references=references,
                unknown_object_references=(),
                dynamic_reference=False,
            ),
        ),
        known_entity_references=references,
        unknown_object_references=(),
        dynamic_paths=(),
        unanalyzed_paths=(),
    )


def test_verifies_exact_recomputed_candidate_impact() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime)
    impact = expand_candidate_impact(dependencies, runtime)

    verify_candidate_impact_analysis(impact, dependencies, runtime)


def test_rejects_tampered_candidate_binding() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime)
    impact = expand_candidate_impact(dependencies, runtime)
    tampered = replace(impact, repository_id=REPOSITORY_ID + 1)

    with pytest.raises(CandidateImpactError, match="does not match"):
        verify_candidate_impact_analysis(tampered, dependencies, runtime)


def test_rejects_removed_runtime_relationship() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime)
    impact = expand_candidate_impact(dependencies, runtime)
    light = next(item for item in impact.entities if item.entity_id == "light.kitchen")
    tampered_light = replace(light, device_ids=())
    tampered = replace(
        impact,
        entities=tuple(tampered_light if item is light else item for item in impact.entities),
    )

    with pytest.raises(CandidateImpactError, match="does not match"):
        verify_candidate_impact_analysis(tampered, dependencies, runtime)


def test_rejects_tampered_unresolved_relationship() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime)
    impact = expand_candidate_impact(dependencies, runtime)
    light = next(item for item in impact.entities if item.entity_id == "light.kitchen")
    assert light.unresolved == ("integration:entry-missing",)
    tampered_light = replace(light, unresolved=())
    tampered = replace(
        impact,
        entities=tuple(tampered_light if item is light else item for item in impact.entities),
    )

    with pytest.raises(CandidateImpactError, match="does not match"):
        verify_candidate_impact_analysis(tampered, dependencies, runtime)


def test_rejects_reordered_candidate_impact() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime)
    impact = expand_candidate_impact(dependencies, runtime)
    tampered = replace(impact, entities=tuple(reversed(impact.entities)))

    with pytest.raises(CandidateImpactError, match="invalid"):
        verify_candidate_impact_analysis(tampered, dependencies, runtime)


def test_rejects_runtime_drift_after_impact_generation() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime)
    impact = expand_candidate_impact(dependencies, runtime)
    changed_runtime = replace(runtime, manifest={"home_assistant_version": "2026.9.2"})

    with pytest.raises(CandidateImpactError, match="runtime binding"):
        verify_candidate_impact_analysis(impact, dependencies, changed_runtime)
