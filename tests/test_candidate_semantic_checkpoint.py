from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_semantic_checkpoint import (
    CandidateSemanticCheckpoint,
    CandidateSemanticCheckpointError,
)
from ha_syncapp.candidate_semantics import validate_candidate_semantics
from semantic_fixtures import candidate_inputs

NOW = datetime(2026, 9, 27, 5, 0, tzinfo=UTC)


def _plan(tmp_path):
    inputs = candidate_inputs(tmp_path, {"configuration.yaml": b"homeassistant:\n"})
    plan = CandidateSemanticCheckpoint.plan(
        candidate_sha=inputs[0].candidate_sha,
        orchestration_sha256="1" * 64,
        fetch_stage_sha256="2" * 64,
        integrity_sha256="3" * 64,
        dependency_sha256="4" * 64,
        risk_sha256="5" * 64,
        static_sha256="6" * 64,
        target=inputs[0].target,
        repository_id=inputs[0].repository_id,
        baseline_sha=inputs[0].baseline_sha,
        stage_manifest_sha256=inputs[0].stage_manifest_sha256,
        runtime_sha256=inputs[0].runtime_sha256,
        core_version=inputs[-1].version,
        planned_at=NOW,
    )
    return inputs, plan


def test_semantic_checkpoint_round_trips_success(tmp_path, monkeypatch) -> None:
    inputs, plan = _plan(tmp_path)
    monkeypatch.setattr("ha_syncapp.candidate_semantics.os.chown", lambda *_args: None)
    monkeypatch.setattr("ha_syncapp.candidate_semantics._run_validator", lambda *_args: None)
    semantic = validate_candidate_semantics(*inputs)
    completed = plan.complete(semantic, *inputs, completed_at=NOW + timedelta(seconds=1))
    restored = CandidateSemanticCheckpoint.from_database_row(completed.database_values())
    assert restored == completed
    assert restored.semantic() == semantic
    assert restored.phase == "completed"


def test_semantic_checkpoint_round_trips_deterministic_block(tmp_path) -> None:
    _, plan = _plan(tmp_path)
    blocked = plan.block(completed_at=NOW + timedelta(seconds=1))
    restored = CandidateSemanticCheckpoint.from_database_row(blocked.database_values())
    assert restored.phase == "blocked"
    with pytest.raises(CandidateSemanticCheckpointError):
        restored.semantic()


def test_semantic_checkpoint_rejects_result_rebound_from_its_plan(tmp_path, monkeypatch) -> None:
    inputs, _ = _plan(tmp_path)
    rebound = CandidateSemanticCheckpoint.plan(
        candidate_sha="c" * 40,
        orchestration_sha256="1" * 64,
        fetch_stage_sha256="2" * 64,
        integrity_sha256="3" * 64,
        dependency_sha256="4" * 64,
        risk_sha256="5" * 64,
        static_sha256="6" * 64,
        target=inputs[0].target,
        repository_id=inputs[0].repository_id,
        baseline_sha=inputs[0].baseline_sha,
        stage_manifest_sha256=inputs[0].stage_manifest_sha256,
        runtime_sha256=inputs[0].runtime_sha256,
        core_version=inputs[-1].version,
        planned_at=NOW,
    )
    monkeypatch.setattr("ha_syncapp.candidate_semantics.os.chown", lambda *_args: None)
    monkeypatch.setattr("ha_syncapp.candidate_semantics._run_validator", lambda *_args: None)
    semantic = validate_candidate_semantics(*inputs)
    with pytest.raises(CandidateSemanticCheckpointError):
        rebound.complete(semantic, *inputs, completed_at=NOW + timedelta(seconds=1))
