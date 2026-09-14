from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ha_syncapp.apply_authorization import authorize_candidate_apply
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.candidate_changes import CandidateChanges
from ha_syncapp.candidate_stage import CandidateStage
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.live_apply_intent_store import (
    discover_live_apply_intents,
    load_live_apply_intent,
    record_live_apply_intent,
)
from ha_syncapp.live_apply_plan import build_live_apply_plan
from ha_syncapp.live_apply_preconditions import prove_live_apply_preconditions
from ha_syncapp.preapply_freshness import reprove_preapply_repo_heads
from ha_syncapp.prepared_deployment import PreparedDeployment
from ha_syncapp.stage_prewrite_reproof import reprove_stage_for_apply
from ha_syncapp.state import StateError, StateStore


def _backup() -> CandidateBackupEvidence:
    return CandidateBackupEvidence(
        target="owner/private-repo",
        repository_id=12345,
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
        stage_manifest_sha256="c" * 64,
        runtime_sha256="d" * 64,
        risk_level="high",
        core_version="2026.9.1",
        backup_slug="backup_123",
    )


def _prepared() -> PreparedDeployment:
    return PreparedDeployment(str(uuid4()), _backup(), datetime.now(UTC))


def _chain(tmp_path: Path, prepared: PreparedDeployment, monkeypatch: pytest.MonkeyPatch):
    evidence = prepared.evidence

    def fetcher(target, token, *, expected_id, branch="main"):
        sha = evidence.baseline_sha if branch == "main" else evidence.candidate_sha
        return BranchHead(target, expected_id, branch, sha)

    freshness = reprove_preapply_repo_heads(
        prepared,
        evidence,
        token="secret-token",
        head_fetcher=fetcher,
    )
    authorization = authorize_candidate_apply(prepared, evidence, freshness)
    stage_root = tmp_path / "candidate-stage"
    stage = CandidateStage(
        root=stage_root,
        tree=stage_root / "tree",
        manifest=stage_root / "manifest.json",
        manifest_sha256=evidence.stage_manifest_sha256,
        target=evidence.target,
        repository_id=evidence.repository_id,
        branch="candidate",
        commit_sha=evidence.candidate_sha,
        entries=(),
    )
    monkeypatch.setattr(
        "ha_syncapp.stage_prewrite_reproof.verify_candidate_stage", lambda _value: None
    )
    stage_evidence = reprove_stage_for_apply(authorization, stage)
    changes = CandidateChanges(
        target=evidence.target,
        repository_id=evidence.repository_id,
        baseline_sha=evidence.baseline_sha,
        candidate_sha=evidence.candidate_sha,
        changes=(),
    )
    monkeypatch.setattr("ha_syncapp.live_apply_plan.verify_candidate_stage", lambda _value: None)
    plan = build_live_apply_plan(stage_evidence, stage, changes)
    live_root = tmp_path / "homeassistant"
    live_root.mkdir(exist_ok=True)
    preconditions = prove_live_apply_preconditions(plan, live_root)
    return authorization, stage_evidence, plan, preconditions


def _prepare_store(store: StateStore, prepared: PreparedDeployment) -> None:
    store.bind_repository(prepared.evidence.target, prepared.evidence.repository_id)
    store.record_prepared_deployment(prepared.deployment_id, prepared.evidence)


def test_exact_chain_persists_idempotently_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared()
    chain = _chain(tmp_path, prepared, monkeypatch)
    first_time = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    later = datetime(2026, 9, 14, 12, 5, tzinfo=UTC)

    with StateStore(tmp_path) as store:
        _prepare_store(store, prepared)
        first = record_live_apply_intent(store, *chain, recorded_at=first_time)
        replay = record_live_apply_intent(store, *chain, recorded_at=later)
        assert replay == first
        assert first.recorded_at == first_time
        assert first.deployment_id == prepared.deployment_id
        assert first.backup_slug == prepared.evidence.backup_slug
        assert first.operations_sha256

    with StateStore(tmp_path) as store:
        loaded = load_live_apply_intent(store, prepared.deployment_id)
        assert loaded == first
        assert discover_live_apply_intents(store) == (first,)


def test_conflicting_exact_deployment_intent_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared()
    authorization, stage_evidence, plan, preconditions = _chain(tmp_path, prepared, monkeypatch)
    with StateStore(tmp_path) as store:
        _prepare_store(store, prepared)
        first = record_live_apply_intent(store, authorization, stage_evidence, plan, preconditions)
        other_root = tmp_path / "other-homeassistant"
        other_root.mkdir()
        other_preconditions = prove_live_apply_preconditions(plan, other_root)
        with pytest.raises(StateError, match="cannot be rebound"):
            record_live_apply_intent(
                store,
                authorization,
                stage_evidence,
                plan,
                other_preconditions,
            )
        assert load_live_apply_intent(store, prepared.deployment_id) == first


def test_tampered_persisted_intent_fails_closed_with_sanitized_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared()
    chain = _chain(tmp_path, prepared, monkeypatch)
    with StateStore(tmp_path) as store:
        _prepare_store(store, prepared)
        record_live_apply_intent(store, *chain)

    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE live_apply_intent SET homeassistant_root = ? WHERE deployment_id = ?",
            ("/PRIVATE-SENTINEL", prepared.deployment_id),
        )

    with StateStore(tmp_path) as store:
        with pytest.raises(StateError) as caught:
            load_live_apply_intent(store, prepared.deployment_id)
        assert "PRIVATE-SENTINEL" not in str(caught.value)
        assert str(caught.value) == "Invalid live Apply intent record"


def test_persist_requires_matching_prepared_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepared()
    chain = _chain(tmp_path, prepared, monkeypatch)
    with StateStore(tmp_path) as store:
        store.bind_repository(prepared.evidence.target, prepared.evidence.repository_id)
        with pytest.raises(StateError, match="prepared deployment"):
            record_live_apply_intent(store, *chain)
