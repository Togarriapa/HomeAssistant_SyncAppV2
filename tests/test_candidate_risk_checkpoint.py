from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from ha_syncapp.candidate_dependencies import CandidateDependencyAnalysis, CandidateDependencyFile
from ha_syncapp.candidate_impact import expand_candidate_impact
from ha_syncapp.candidate_risk import classify_candidate_risk
from ha_syncapp.candidate_risk_checkpoint import (
    CandidateRiskCheckpoint,
    CandidateRiskCheckpointError,
)
from ha_syncapp.runtime_evidence import fingerprint_runtime
from ha_syncapp.runtime_inventory import RuntimeInventoryInput

NOW = datetime(2026, 9, 27, 0, 0, tzinfo=UTC)
HASH = "c" * 64


def _evidence():
    runtime = RuntimeInventoryInput(
        manifest={"source": "candidate-risk"},
        homeassistant={"entities": [{"entity_id": "light.kitchen"}], "services": []},
    )
    dependencies = CandidateDependencyAnalysis(
        "owner/home", 42, "b" * 40, "a" * 40, HASH, fingerprint_runtime(runtime),
        "best_effort_lexical",
        (CandidateDependencyFile("automations.yaml", "analyzed_text", ("light.kitchen",), (), False, ()),),
        ("light.kitchen",), (), (), (), (),
    )
    impact = expand_candidate_impact(dependencies, runtime)
    risk = classify_candidate_risk(dependencies, impact, runtime)
    return runtime, dependencies, impact, risk


def test_completed_checkpoint_round_trips_verified_impact_and_risk() -> None:
    runtime, dependencies, impact, risk = _evidence()
    planned = CandidateRiskCheckpoint.plan(
        candidate_sha="a" * 40,
        orchestration_sha256=HASH,
        dependency_sha256="d" * 64,
        target="owner/home",
        repository_id=42,
        baseline_sha="b" * 40,
        stage_manifest_sha256=HASH,
        planned_at=NOW,
    )
    completed = planned.complete(impact, risk, dependencies, runtime, completed_at=NOW)
    assert completed.impact(dependencies, runtime) == impact
    assert completed.risk(dependencies, runtime) == risk
    assert CandidateRiskCheckpoint.from_database_row(completed.database_values()) == completed


def test_checkpoint_tampering_fails_closed() -> None:
    runtime, dependencies, impact, risk = _evidence()
    completed = CandidateRiskCheckpoint.plan(
        candidate_sha="a" * 40, orchestration_sha256=HASH, dependency_sha256="d" * 64,
        target="owner/home", repository_id=42, baseline_sha="b" * 40,
        stage_manifest_sha256=HASH, planned_at=NOW,
    ).complete(impact, risk, dependencies, runtime, completed_at=NOW)
    with pytest.raises(CandidateRiskCheckpointError):
        replace(completed, risk_level="critical").validate()
