from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ha_syncapp.candidate_dependencies import (
    CandidateDependencyAnalysis,
    CandidateDependencyFile,
)
from ha_syncapp.candidate_dependency_checkpoint import (
    CandidateDependencyCheckpoint,
    CandidateDependencyCheckpointError,
)
from ha_syncapp.runtime_evidence import fingerprint_runtime
from ha_syncapp.runtime_inventory import RuntimeInventoryInput

NOW = datetime(2026, 9, 26, 19, 0, tzinfo=UTC)
CANDIDATE_SHA = "a" * 40
BASELINE_SHA = "b" * 40
HASH = "c" * 64


def _runtime() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={"source": "candidate_dependency"},
        homeassistant={
            "entities": [{"entity_id": "light.kitchen"}],
            "services": [{"domain": "light", "services": {"turn_on": {}}}],
        },
    )


def _analysis(runtime: RuntimeInventoryInput) -> CandidateDependencyAnalysis:
    file = CandidateDependencyFile(
        "automations.yaml",
        "analyzed_text",
        ("light.kitchen",),
        (),
        False,
        ("light.turn_on",),
    )
    return CandidateDependencyAnalysis(
        "owner/home",
        42,
        BASELINE_SHA,
        CANDIDATE_SHA,
        HASH,
        fingerprint_runtime(runtime),
        "best_effort_lexical",
        (file,),
        ("light.kitchen",),
        (),
        (),
        (),
        ("light.turn_on",),
    )


def test_completed_checkpoint_round_trips_canonical_runtime_and_dependency_evidence() -> None:
    runtime = _runtime()
    planned = CandidateDependencyCheckpoint.plan(
        candidate_sha=CANDIDATE_SHA,
        orchestration_sha256=HASH,
        fetch_stage_sha256="d" * 64,
        integrity_sha256="e" * 64,
        target="owner/home",
        repository_id=42,
        baseline_sha=BASELINE_SHA,
        stage_manifest_sha256=HASH,
        planned_at=NOW,
    )

    completed = planned.complete(_analysis(runtime), runtime, completed_at=NOW)

    assert completed.phase == "completed"
    assert completed.dependencies() == _analysis(runtime)
    assert completed.runtime() == runtime
    assert (
        completed.database_values()
        == CandidateDependencyCheckpoint.from_database_row(
            completed.database_values()
        ).database_values()
    )


def test_checkpoint_tampering_fails_closed() -> None:
    runtime = _runtime()
    completed = CandidateDependencyCheckpoint.plan(
        candidate_sha=CANDIDATE_SHA,
        orchestration_sha256=HASH,
        fetch_stage_sha256="d" * 64,
        integrity_sha256="e" * 64,
        target="owner/home",
        repository_id=42,
        baseline_sha=BASELINE_SHA,
        stage_manifest_sha256=HASH,
        planned_at=NOW,
    ).complete(_analysis(runtime), runtime, completed_at=NOW)
    row = list(completed.database_values())
    row[12] = str(row[12]).replace("light.kitchen", "light.garage")

    with pytest.raises(CandidateDependencyCheckpointError):
        CandidateDependencyCheckpoint.from_database_row(tuple(row))
