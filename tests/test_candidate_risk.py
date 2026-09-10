from __future__ import annotations

from dataclasses import replace

import pytest
from ha_syncapp.candidate_dependencies import (
    CandidateDependencyAnalysis,
    CandidateDependencyFile,
)
from ha_syncapp.candidate_impact import CandidateImpactAnalysis, expand_candidate_impact
from ha_syncapp.candidate_risk import (
    CandidateRiskError,
    classify_candidate_risk,
    verify_candidate_risk_classification,
)
from ha_syncapp.runtime_evidence import fingerprint_runtime
from ha_syncapp.runtime_inventory import RuntimeInventoryInput

TARGET = "Owner/Home"
REPOSITORY_ID = 42
BASELINE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
MANIFEST_SHA = "c" * 64


def _runtime(*, missing_integration: bool = False) -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={"home_assistant_version": "2026.9.1"},
        homeassistant={
            "entities": [
                {
                    "entity_id": "light.kitchen",
                    "device_id": "device-1",
                    "config_entry_id": "missing-entry" if missing_integration else "entry-1",
                    "labels": [],
                }
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
            "areas": [{"id": "area-1", "floor_id": None, "labels": []}],
            "floors": [],
            "labels": [],
            "states": [{"entity_id": "light.kitchen", "state": "on"}],
            "services": [
                {"domain": "light", "services": {"turn_on": {}}},
            ],
        },
    )


def _dependencies(
    runtime: RuntimeInventoryInput,
    path: str,
    *,
    known: tuple[str, ...] = ("light.kitchen",),
    services: tuple[str, ...] = (),
    unknown: tuple[str, ...] = (),
    dynamic: bool = False,
    disposition: str = "analyzed_text",
) -> CandidateDependencyAnalysis:
    file = CandidateDependencyFile(
        path=path,
        disposition=disposition,
        known_entity_references=known if disposition == "analyzed_text" else (),
        unknown_object_references=unknown if disposition == "analyzed_text" else (),
        dynamic_reference=dynamic if disposition == "analyzed_text" else False,
        known_service_references=services if disposition == "analyzed_text" else (),
    )
    return CandidateDependencyAnalysis(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        stage_manifest_sha256=MANIFEST_SHA,
        runtime_sha256=fingerprint_runtime(runtime),
        analysis_method="best_effort_lexical",
        files=(file,),
        known_entity_references=file.known_entity_references,
        unknown_object_references=file.unknown_object_references,
        dynamic_paths=(path,) if file.dynamic_reference else (),
        unanalyzed_paths=(path,) if disposition in {"non_utf8_or_binary", "oversize"} else (),
        known_service_references=file.known_service_references,
    )


def _impact(
    dependencies: CandidateDependencyAnalysis,
    runtime: RuntimeInventoryInput,
) -> CandidateImpactAnalysis:
    return expand_candidate_impact(dependencies, runtime)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("automations.yaml", "low"),
        ("scripts.yaml", "low"),
        ("scenes.yaml", "low"),
        ("integrations/lighting.yaml", "medium"),
        ("packages/lighting.yaml", "medium"),
        ("dashboards/tablet.yaml", "medium"),
        ("configuration.yaml", "high"),
        ("secrets.yaml", "high"),
        (".storage/core.config_entries", "high"),
        ("custom_components/example/manifest.json", "high"),
        ("home-assistant_v2.db", "critical"),
        ("home-assistant_v2.db-wal", "critical"),
        ("systemd/home-assistant.service", "critical"),
    ],
)
def test_readme_path_examples_establish_risk_floor(path: str, expected: str) -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime, path)

    result = classify_candidate_risk(dependencies, _impact(dependencies, runtime), runtime)

    assert result.level == expected
    assert result.changed_paths == (path,)
    verify_candidate_risk_classification(
        result,
        dependencies,
        _impact(dependencies, runtime),
        runtime,
    )


def test_known_service_reference_does_not_trigger_unknown_object_escalation() -> None:
    runtime = _runtime()
    dependencies = _dependencies(
        runtime,
        "automations.yaml",
        known=(),
        services=("light.turn_on",),
    )

    result = classify_candidate_risk(dependencies, _impact(dependencies, runtime), runtime)

    assert result.level == "low"
    assert result.unresolved_references == ()
    assert "unknown candidate object references require conservative handling" not in result.reasons


def test_dynamic_candidate_reference_raises_low_path_to_high() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime, "automations.yaml", dynamic=True)

    result = classify_candidate_risk(dependencies, _impact(dependencies, runtime), runtime)

    assert result.level == "high"
    assert result.dynamic_paths == ("automations.yaml",)
    assert "dynamic candidate references require conservative handling" in result.reasons


def test_unknown_reference_raises_low_path_to_high() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime, "scripts.yaml", unknown=("notify.family",))

    result = classify_candidate_risk(dependencies, _impact(dependencies, runtime), runtime)

    assert result.level == "high"
    assert result.unresolved_references == ("notify.family",)


def test_unanalyzed_candidate_bytes_raise_risk_to_high() -> None:
    runtime = _runtime()
    dependencies = _dependencies(
        runtime,
        "automations.yaml",
        known=(),
        disposition="non_utf8_or_binary",
    )

    result = classify_candidate_risk(dependencies, _impact(dependencies, runtime), runtime)

    assert result.level == "high"
    assert result.unanalyzed_paths == ("automations.yaml",)


def test_unresolved_runtime_relationship_raises_risk_to_high() -> None:
    runtime = _runtime(missing_integration=True)
    dependencies = _dependencies(runtime, "automations.yaml")

    result = classify_candidate_risk(dependencies, _impact(dependencies, runtime), runtime)

    assert result.level == "high"
    assert result.unresolved_references == ("integration:missing-entry",)


def test_critical_path_cannot_be_lowered_by_other_evidence() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime, "recorder.sqlite3", known=())

    result = classify_candidate_risk(dependencies, _impact(dependencies, runtime), runtime)

    assert result.level == "critical"


def test_tampered_impact_is_rejected_before_classification() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime, "automations.yaml")
    impact = _impact(dependencies, runtime)
    tampered = replace(impact, repository_id=REPOSITORY_ID + 1)

    with pytest.raises(CandidateRiskError, match="impact"):
        classify_candidate_risk(dependencies, tampered, runtime)


def test_risk_verifier_rejects_tampered_result() -> None:
    runtime = _runtime()
    dependencies = _dependencies(runtime, "automations.yaml")
    impact = _impact(dependencies, runtime)
    result = classify_candidate_risk(dependencies, impact, runtime)
    tampered = replace(result, level="medium")

    with pytest.raises(CandidateRiskError, match="does not match"):
        verify_candidate_risk_classification(tampered, dependencies, impact, runtime)
