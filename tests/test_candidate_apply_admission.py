from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp.candidate_apply_admission import (
    CandidateApplyAdmissionError,
    execute_candidate_apply_admission_once,
)
from ha_syncapp.candidate_backup import CandidateBackupError
from ha_syncapp.candidate_backup_execution import execute_candidate_backup_once
from ha_syncapp.candidate_dependency_execution import load_candidate_dependency_checkpoint
from ha_syncapp.candidate_integrity_execution import load_candidate_integrity_checkpoint
from ha_syncapp.candidate_risk_execution import load_candidate_risk_checkpoint
from ha_syncapp.candidate_static_execution import load_candidate_static_checkpoint
from ha_syncapp.live_apply_intent_store import load_live_apply_intent
from ha_syncapp.live_apply_preconditions import LiveApplyPreconditionError
from test_candidate_backup_execution import NOW, _backup_ready, _evidence
from test_live_apply_intent_store import _chain


def _prepared_apply(tmp_path, monkeypatch):
    store, orchestration, semantic, authority = _backup_ready(tmp_path, monkeypatch)
    result = execute_candidate_backup_once(
        store,
        orchestration,
        staging_root=tmp_path,
        home_assistant_root=tmp_path,
        token="supervisor-token",
        creator=lambda *args, **kwargs: _evidence(semantic),
        now=NOW + timedelta(seconds=5),
    )
    claimed = store.claim_work_kind("candidate_apply", now=NOW + timedelta(seconds=6))
    assert result.prepared is not None and claimed is not None
    return store, result.prepared, authority, claimed


def test_exact_preapply_chain_is_reproved_before_atomic_admission(tmp_path, monkeypatch) -> None:
    store, prepared, authority, claimed = _prepared_apply(tmp_path, monkeypatch)
    chain = _chain(tmp_path, prepared, monkeypatch)
    calls: list[str] = []

    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.load_candidate_backup_authority",
        lambda *args, **kwargs: authority,
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.reprove_prepared_candidate_backup",
        lambda *args, **kwargs: calls.append("backup") or prepared.evidence,
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.reprove_preapply_repo_heads",
        lambda *args, **kwargs: calls.append("heads") or object(),
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.authorize_candidate_apply",
        lambda *args: calls.append("authorize") or chain[0],
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.reprove_stage_for_apply",
        lambda *args: calls.append("stage") or chain[1],
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.build_live_apply_plan",
        lambda *args: calls.append("plan") or chain[2],
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.prove_live_apply_preconditions",
        lambda *args: calls.append("preconditions") or chain[3],
    )
    try:
        result = execute_candidate_apply_admission_once(
            store,
            claimed,
            staging_root=tmp_path,
            home_assistant_root=tmp_path / "homeassistant",
            github_token="github-token",
            supervisor_token="supervisor-token",
            now=NOW + timedelta(seconds=7),
        )

        assert calls == ["backup", "heads", "authorize", "stage", "plan", "preconditions"]
        assert result.intent == load_live_apply_intent(store, prepared.deployment_id)
        assert result.deployment_id == prepared.deployment_id
        assert store._get_work("candidate_apply", prepared.deployment_id).status == "succeeded"
    finally:
        store.__exit__(None, None, None)


def test_local_precondition_failure_is_deterministic_and_persists_nothing(
    tmp_path, monkeypatch
) -> None:
    store, prepared, authority, claimed = _prepared_apply(tmp_path, monkeypatch)
    chain = _chain(tmp_path, prepared, monkeypatch)
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.load_candidate_backup_authority",
        lambda *args, **kwargs: authority,
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.reprove_prepared_candidate_backup",
        lambda *args, **kwargs: prepared.evidence,
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.reprove_preapply_repo_heads",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.authorize_candidate_apply",
        lambda *args: chain[0],
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.reprove_stage_for_apply",
        lambda *args: chain[1],
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.build_live_apply_plan",
        lambda *args: chain[2],
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.prove_live_apply_preconditions",
        lambda *args: (_ for _ in ()).throw(LiveApplyPreconditionError("changed")),
    )
    try:
        with pytest.raises(CandidateApplyAdmissionError) as caught:
            execute_candidate_apply_admission_once(
                store,
                claimed,
                staging_root=tmp_path,
                home_assistant_root=tmp_path / "homeassistant",
                github_token="github-token",
                supervisor_token="supervisor-token",
                now=NOW + timedelta(seconds=7),
            )

        assert not caught.value.transient
        assert load_live_apply_intent(store, prepared.deployment_id) is None
        assert store._get_work("candidate_apply", prepared.deployment_id).status == "running"
    finally:
        store.__exit__(None, None, None)


def test_supervisor_reproof_failure_remains_retryable(tmp_path, monkeypatch) -> None:
    store, prepared, authority, claimed = _prepared_apply(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.load_candidate_backup_authority",
        lambda *args, **kwargs: authority,
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_admission.reprove_prepared_candidate_backup",
        lambda *args, **kwargs: (_ for _ in ()).throw(CandidateBackupError("unavailable")),
    )
    try:
        with pytest.raises(CandidateApplyAdmissionError) as caught:
            execute_candidate_apply_admission_once(
                store,
                claimed,
                staging_root=tmp_path,
                home_assistant_root=tmp_path,
                github_token="github-token",
                supervisor_token="supervisor-token",
            )

        assert caught.value.transient
        assert load_live_apply_intent(store, prepared.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_completed_candidate_keeps_upstream_authority_loadable(tmp_path, monkeypatch) -> None:
    store, prepared, _authority, _claimed = _prepared_apply(tmp_path, monkeypatch)
    candidate_sha = prepared.evidence.candidate_sha
    try:
        integrity = load_candidate_integrity_checkpoint(store, candidate_sha)
        dependency = load_candidate_dependency_checkpoint(store, candidate_sha)
        risk = load_candidate_risk_checkpoint(store, candidate_sha)
        static = load_candidate_static_checkpoint(store, candidate_sha)

        assert integrity is not None and integrity.phase == "completed"
        assert dependency is not None and dependency.phase == "completed"
        assert risk is not None and risk.phase == "completed"
        assert static is not None and static.phase == "completed"
    finally:
        store.__exit__(None, None, None)
