from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import ha_syncapp.candidate_validation as validation_module
import pytest
from ha_syncapp.candidate_static_checkpoint import (
    CandidateStaticCheckpoint,
    CandidateStaticCheckpointError,
)
from test_candidate_validation import CANDIDATE_SHA, TARGET, _evidence, _stage

NOW = datetime(2026, 9, 27, 1, 0, tzinfo=UTC)
HASH = "f" * 64


def test_checkpoint_round_trips_exact_verified_static_result(tmp_path, monkeypatch) -> None:
    stage = _stage(tmp_path, {"automations.yaml": b"- alias: safe\n"})
    integrity, dependencies, impact, risk, runtime = _evidence(("automations.yaml",))
    monkeypatch.setattr(validation_module.stage_module, "verify_candidate_stage", lambda value: None)
    monkeypatch.setattr(validation_module, "verify_candidate_risk_classification", lambda *args: None)
    result = validation_module.validate_candidate_configuration(
        integrity, stage, dependencies, impact, risk, runtime
    )
    completed = CandidateStaticCheckpoint.plan(
        candidate_sha=CANDIDATE_SHA,
        orchestration_sha256=HASH,
        fetch_stage_sha256="a" * 64,
        integrity_sha256="b" * 64,
        dependency_sha256="c" * 64,
        risk_sha256="d" * 64,
        target=TARGET,
        repository_id=42,
        baseline_sha="a" * 40,
        stage_manifest_sha256="c" * 64,
        planned_at=NOW,
    ).complete(
        result, integrity, stage, dependencies, impact, risk, runtime, completed_at=NOW
    )
    assert completed.validation() == result
    assert CandidateStaticCheckpoint.from_database_row(completed.database_values()) == completed


def test_checkpoint_tampering_fails_closed(tmp_path, monkeypatch) -> None:
    stage = _stage(tmp_path, {"automations.yaml": b"broken: [yaml\n"})
    integrity, dependencies, impact, risk, runtime = _evidence(("automations.yaml",))
    monkeypatch.setattr(validation_module.stage_module, "verify_candidate_stage", lambda value: None)
    monkeypatch.setattr(validation_module, "verify_candidate_risk_classification", lambda *args: None)
    result = validation_module.validate_candidate_configuration(
        integrity, stage, dependencies, impact, risk, runtime
    )
    completed = CandidateStaticCheckpoint.plan(
        candidate_sha=CANDIDATE_SHA, orchestration_sha256=HASH,
        fetch_stage_sha256="a" * 64, integrity_sha256="b" * 64,
        dependency_sha256="c" * 64, risk_sha256="d" * 64,
        target=TARGET, repository_id=42, baseline_sha="a" * 40,
        stage_manifest_sha256="c" * 64, planned_at=NOW,
    ).complete(result, integrity, stage, dependencies, impact, risk, runtime, completed_at=NOW)
    with pytest.raises(CandidateStaticCheckpointError):
        replace(completed, syntax_valid=True).validate()
