from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import ha_syncapp.candidate_semantic_execution as execution
import pytest
from ha_syncapp.candidate_fetch_stage_execution import load_candidate_fetch_stage_checkpoint
from ha_syncapp.candidate_semantics import CandidateSemanticError, CandidateSemanticValidation
from ha_syncapp.candidate_static_execution import execute_candidate_static_once
from semantic_fixtures import candidate_inputs
from test_candidate_integrity_execution import NOW
from test_candidate_static_execution import _static_ready


def _semantic_ready(tmp_path, monkeypatch):
    store, orchestration = _static_ready(tmp_path, monkeypatch, b"- alias: safe\n")
    static_result = execute_candidate_static_once(
        store,
        orchestration,
        staging_root=tmp_path,
        home_assistant_root=tmp_path,
        now=NOW + timedelta(seconds=3),
    )
    static, integrity, stage, dependencies, impact, risk, runtime, version = candidate_inputs(
        tmp_path / "semantic", {"configuration.yaml": b"homeassistant:\n"}
    )
    fetch = load_candidate_fetch_stage_checkpoint(store, orchestration.candidate_sha)
    assert fetch is not None
    monkeypatch.setattr(
        execution, "load_completed_candidate_stage", lambda *a, **k: (fetch, stage)
    )
    monkeypatch.setattr(
        execution,
        "load_candidate_integrity_checkpoint",
        lambda *a: SimpleNamespace(
            record_sha256="1" * 64,
            changes=lambda: SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(execution, "_integrity_from_checkpoint", lambda *a: integrity)
    monkeypatch.setattr(
        execution,
        "load_candidate_dependency_checkpoint",
        lambda *a: SimpleNamespace(
            record_sha256="2" * 64,
            dependencies=lambda: dependencies,
            runtime=lambda: runtime,
        ),
    )
    monkeypatch.setattr(
        execution,
        "load_candidate_risk_checkpoint",
        lambda *a: SimpleNamespace(
            record_sha256="3" * 64,
            impact=lambda *a: impact,
            risk=lambda *a: risk,
        ),
    )
    monkeypatch.setattr(
        execution,
        "load_candidate_static_checkpoint",
        lambda *a: SimpleNamespace(record_sha256="4" * 64, validation=lambda: static),
    )
    monkeypatch.setattr(execution, "bind_core_version", lambda value: version)
    expected = CandidateSemanticValidation(
        static.target,
        static.repository_id,
        static.baseline_sha,
        static.candidate_sha,
        static.stage_manifest_sha256,
        static.runtime_sha256,
        static.risk_level,
        version.version,
    )
    return store, static_result.orchestration, expected


def test_success_is_journaled_and_replays_without_validator(tmp_path, monkeypatch) -> None:
    store, orchestration, expected = _semantic_ready(tmp_path, monkeypatch)
    calls = []
    try:
        result = execution.execute_candidate_semantic_once(
            store,
            orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            validator=lambda *args: calls.append(args) or expected,
            now=NOW + timedelta(seconds=4),
        )
        assert result.semantic == expected
        assert result.checkpoint.phase == "completed"
        assert result.orchestration.phase == "semantically_validated"
        assert result.orchestration.next_action == "prepare_backup"
        replay = execution.execute_candidate_semantic_once(
            store,
            result.orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            validator=lambda *args: pytest.fail("completed replay must not execute validator"),
            now=NOW + timedelta(seconds=5),
        )
        assert replay.replayed
        assert replay.semantic == expected
        assert len(calls) == 1
    finally:
        store.__exit__(None, None, None)


def test_transient_failure_keeps_plan_retryable(tmp_path, monkeypatch) -> None:
    store, orchestration, expected = _semantic_ready(tmp_path, monkeypatch)
    try:
        with pytest.raises(execution.CandidateSemanticExecutionError) as caught:
            execution.execute_candidate_semantic_once(
                store,
                orchestration,
                staging_root=tmp_path,
                home_assistant_root=tmp_path,
                validator=lambda *args: (_ for _ in ()).throw(
                    CandidateSemanticError("validator unavailable", transient=True)
                ),
                now=NOW + timedelta(seconds=4),
            )
        assert caught.value.transient
        checkpoint = execution.load_candidate_semantic_checkpoint(
            store, orchestration.candidate_sha
        )
        assert checkpoint is not None and checkpoint.phase == "planned"
        retried = execution.execute_candidate_semantic_once(
            store,
            orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            validator=lambda *args: expected,
            now=NOW + timedelta(seconds=5),
        )
        assert retried.checkpoint.phase == "completed"
    finally:
        store.__exit__(None, None, None)


def test_deterministic_failure_blocks_and_replays_without_validator(tmp_path, monkeypatch) -> None:
    store, orchestration, _expected = _semantic_ready(tmp_path, monkeypatch)
    try:
        result = execution.execute_candidate_semantic_once(
            store,
            orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            validator=lambda *args: (_ for _ in ()).throw(
                CandidateSemanticError("unsupported exact candidate")
            ),
            now=NOW + timedelta(seconds=4),
        )
        assert result.semantic is None
        assert result.checkpoint.phase == "blocked"
        assert result.orchestration.phase == "blocked"
        assert store._get_work("candidate", orchestration.candidate_sha).status == "blocked"
        replay = execution.execute_candidate_semantic_once(
            store,
            result.orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            validator=lambda *args: pytest.fail("blocked replay must not execute validator"),
            now=NOW + timedelta(seconds=5),
        )
        assert replay.replayed
        assert replay.semantic is None
    finally:
        store.__exit__(None, None, None)
