from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import ha_syncapp.candidate_static_execution as execution
from ha_syncapp.candidate_fetch_stage_execution import load_candidate_fetch_stage_checkpoint
from ha_syncapp.candidate_risk_execution import load_candidate_risk_checkpoint
from ha_syncapp.runtime_evidence import fingerprint_runtime
from test_candidate_integrity_execution import NOW
from test_candidate_risk_execution import _ready_for_risk
from test_candidate_validation import _evidence, _stage


def _static_ready(tmp_path, monkeypatch, content: bytes):
    store, upstream = _ready_for_risk(tmp_path)
    risk_checkpoint = load_candidate_risk_checkpoint(store, upstream.orchestration.candidate_sha)
    fetch_checkpoint = load_candidate_fetch_stage_checkpoint(
        store, upstream.orchestration.candidate_sha
    )
    assert risk_checkpoint is not None and fetch_checkpoint is not None
    stage = _stage(tmp_path / "static", {"automations.yaml": content})
    integrity, dependencies, impact, risk, runtime = _evidence(("automations.yaml",))
    manifest = stage.manifest_sha256
    bindings = {
        "target": upstream.orchestration.target,
        "repository_id": upstream.orchestration.repository_id,
        "candidate_sha": upstream.orchestration.candidate_sha,
        "baseline_sha": upstream.checkpoint.baseline_sha,
        "stage_manifest_sha256": manifest,
    }
    stage = replace(
        stage,
        target=bindings["target"],
        repository_id=bindings["repository_id"],
        commit_sha=bindings["candidate_sha"],
    )
    integrity = replace(integrity, **bindings)
    runtime_sha = fingerprint_runtime(runtime)
    dependencies = replace(dependencies, **bindings, runtime_sha256=runtime_sha)
    impact = replace(impact, **bindings, runtime_sha256=runtime_sha)
    risk = replace(risk, **bindings, runtime_sha256=runtime_sha)
    monkeypatch.setattr(execution, "load_completed_candidate_stage", lambda *a, **k: (
        fetch_checkpoint, stage
    ))
    monkeypatch.setattr(execution, "load_candidate_integrity_checkpoint", lambda *a: SimpleNamespace(
        record_sha256="1" * 64, baseline_sha=bindings["baseline_sha"],
        stage_manifest_sha256=manifest, changes=lambda: SimpleNamespace()
    ))
    monkeypatch.setattr(execution, "load_candidate_dependency_checkpoint", lambda *a: SimpleNamespace(
        record_sha256="2" * 64, dependencies=lambda: dependencies, runtime=lambda: runtime
    ))
    monkeypatch.setattr(execution, "load_candidate_risk_checkpoint", lambda *a: SimpleNamespace(
        record_sha256=risk_checkpoint.record_sha256, impact=lambda *x: impact,
        risk=lambda *x: risk
    ))
    monkeypatch.setattr(execution, "_integrity_from_checkpoint", lambda *a: integrity)
    monkeypatch.setattr(execution.stage_module, "verify_candidate_stage", lambda value: None)
    monkeypatch.setattr(execution.validation_module, "verify_candidate_risk_classification", lambda *a: None)
    return store, upstream.orchestration


def test_valid_static_result_advances_only_to_semantic_validation(tmp_path, monkeypatch) -> None:
    store, orchestration = _static_ready(tmp_path, monkeypatch, b"- alias: safe\n")
    try:
        result = execution.execute_candidate_static_once(
            store, orchestration, staging_root=tmp_path, home_assistant_root=tmp_path,
            now=NOW + timedelta(seconds=3),
        )
        assert result.validation.syntax_valid
        assert result.orchestration.phase == "static_validated"
        assert result.orchestration.next_action == "validate_semantics"
    finally:
        store.__exit__(None, None, None)


def test_invalid_static_result_blocks_exact_candidate(tmp_path, monkeypatch) -> None:
    store, orchestration = _static_ready(tmp_path, monkeypatch, b"broken: [yaml\n")
    try:
        result = execution.execute_candidate_static_once(
            store, orchestration, staging_root=tmp_path, home_assistant_root=tmp_path,
            now=NOW + timedelta(seconds=3),
        )
        assert not result.validation.syntax_valid
        assert result.orchestration.phase == "blocked"
        assert result.orchestration.next_action == "none"
        assert store._get_work("candidate", orchestration.candidate_sha).status == "blocked"
    finally:
        store.__exit__(None, None, None)
