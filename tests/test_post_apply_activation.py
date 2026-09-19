from __future__ import annotations

import hashlib
import inspect
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ha_syncapp import live_apply_writer
from ha_syncapp.apply_authorization import ApplyAuthorization
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.candidate_stage import CandidateStage, CandidateStageEntry
from ha_syncapp.live_apply_intent_store import record_live_apply_intent
from ha_syncapp.live_apply_plan import LiveApplyOperation, LiveApplyPlan
from ha_syncapp.live_apply_preconditions import prove_live_apply_preconditions
from ha_syncapp.live_apply_writer import apply_live_operation
from ha_syncapp.post_apply_activation import (
    PostApplyActivationAuthorization,
    PostApplyActivationError,
    authorize_post_apply_activation,
    discover_post_apply_activation_authorizations,
    load_post_apply_activation_authorization,
)
from ha_syncapp.prepared_deployment import PreparedDeployment
from ha_syncapp.stage_prewrite_reproof import StagePrewriteEvidence
from ha_syncapp.state import StateStore


def _blob_id(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def _chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, empty: bool = False):
    live = tmp_path / "homeassistant"
    live.mkdir()
    (live / "automations.yaml").write_bytes(b"baseline\n")
    os.chmod(live / "automations.yaml", 0o644)
    candidate = b"candidate\n"
    operation = LiveApplyOperation(
        path="automations.yaml",
        status="modified",
        baseline_mode="100644",
        baseline_object_id=_blob_id(b"baseline\n"),
        candidate_mode="100644",
        candidate_object_id=_blob_id(candidate),
        staged_size=len(candidate),
        staged_sha256=hashlib.sha256(candidate).hexdigest(),
    )
    backup = CandidateBackupEvidence(
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
    prepared = PreparedDeployment(str(uuid4()), backup, datetime.now(UTC))
    authorization = object.__new__(ApplyAuthorization)
    for name, value in {
        "deployment_id": prepared.deployment_id,
        "target": backup.target,
        "repository_id": backup.repository_id,
        "baseline_sha": backup.baseline_sha,
        "candidate_sha": backup.candidate_sha,
        "backup_slug": backup.backup_slug,
        "stage_manifest_sha256": backup.stage_manifest_sha256,
        "runtime_sha256": backup.runtime_sha256,
        "risk_level": backup.risk_level,
        "core_version": backup.core_version,
    }.items():
        object.__setattr__(authorization, name, value)
    stage_root = tmp_path / "stage"
    tree = stage_root / "tree"
    tree.mkdir(parents=True)
    staged = tree / "automations.yaml"
    staged.write_bytes(candidate)
    os.chmod(staged, 0o600)
    stage = CandidateStage(
        root=stage_root,
        tree=tree,
        manifest=stage_root / "manifest.json",
        manifest_sha256=backup.stage_manifest_sha256,
        target=backup.target,
        repository_id=backup.repository_id,
        branch="candidate",
        commit_sha=backup.candidate_sha,
        entries=(
            CandidateStageEntry(
                path="automations.yaml",
                git_mode="100644",
                object_id=_blob_id(candidate),
                size=len(candidate),
                sha256=hashlib.sha256(candidate).hexdigest(),
            ),
        ),
    )
    stage_evidence = object.__new__(StagePrewriteEvidence)
    for name, value in {
        "deployment_id": prepared.deployment_id,
        "target": backup.target,
        "repository_id": backup.repository_id,
        "candidate_sha": backup.candidate_sha,
        "stage_manifest_sha256": backup.stage_manifest_sha256,
    }.items():
        object.__setattr__(stage_evidence, name, value)
    plan = object.__new__(LiveApplyPlan)
    for name, value in {
        "deployment_id": prepared.deployment_id,
        "target": backup.target,
        "repository_id": backup.repository_id,
        "baseline_sha": backup.baseline_sha,
        "candidate_sha": backup.candidate_sha,
        "stage_manifest_sha256": backup.stage_manifest_sha256,
        "operations": () if empty else (operation,),
    }.items():
        object.__setattr__(plan, name, value)
    preconditions = prove_live_apply_preconditions(plan, live)
    state_root = tmp_path / "state"
    state_root.mkdir()
    store = StateStore(state_root)
    store.__enter__()
    store.bind_repository(backup.target, backup.repository_id)
    store.record_prepared_deployment(prepared.deployment_id, backup)
    record_live_apply_intent(store, authorization, stage_evidence, plan, preconditions)
    monkeypatch.setattr("ha_syncapp.live_apply_writer.verify_candidate_stage", lambda _stage: None)
    monkeypatch.setattr(
        "ha_syncapp.post_apply_activation.verify_candidate_stage", lambda _stage: None
    )
    return store, authorization, stage_evidence, stage, plan, preconditions


def test_complete_apply_is_durably_authorized_idempotently(tmp_path: Path, monkeypatch) -> None:
    chain = _chain(tmp_path, monkeypatch)
    store, authorization, stage_evidence, stage, plan, preconditions = chain
    try:
        apply_live_operation(
            store, authorization, stage_evidence, stage, plan, preconditions, operation_index=0
        )
        first = authorize_post_apply_activation(store, *chain[1:])
        replay = authorize_post_apply_activation(store, *chain[1:])

        assert first.action == "restart_core"
        assert first.authorization is not None
        assert replay.authorization == first.authorization
        assert replay.replayed is True
        assert (
            load_post_apply_activation_authorization(store, plan.deployment_id)
            == first.authorization
        )
        assert discover_post_apply_activation_authorizations(store) == (first.authorization,)
    finally:
        store.__exit__(None, None, None)


def test_incomplete_apply_cannot_authorize_activation(tmp_path: Path, monkeypatch) -> None:
    chain = _chain(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(PostApplyActivationError, match="not complete"):
            authorize_post_apply_activation(store, *chain[1:])
        assert load_post_apply_activation_authorization(store, chain[4].deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_empty_plan_is_no_activation_required_and_is_not_persisted(
    tmp_path: Path, monkeypatch
) -> None:
    chain = _chain(tmp_path, monkeypatch, empty=True)
    store = chain[0]
    try:
        result = authorize_post_apply_activation(store, *chain[1:])
        assert result.action == "no_activation_required"
        assert result.authorization is None
        assert load_post_apply_activation_authorization(store, chain[4].deployment_id) is None
        assert discover_post_apply_activation_authorizations(store) == ()
    finally:
        store.__exit__(None, None, None)


def test_corrupt_verified_progress_fails_closed(tmp_path: Path, monkeypatch) -> None:
    chain = _chain(tmp_path, monkeypatch)
    store, authorization, stage_evidence, stage, plan, preconditions = chain
    try:
        apply_live_operation(
            store, authorization, stage_evidence, stage, plan, preconditions, operation_index=0
        )
        store._connection.execute(
            "UPDATE live_apply_progress SET operation_path_sha256 = ? WHERE deployment_id = ?",
            ("f" * 64, plan.deployment_id),
        )
        store._connection.commit()
        with pytest.raises(PostApplyActivationError, match="invalid"):
            authorize_post_apply_activation(store, *chain[1:])
    finally:
        store.__exit__(None, None, None)


def test_uncertain_apply_cannot_authorize_activation(tmp_path: Path, monkeypatch) -> None:
    chain = _chain(tmp_path, monkeypatch)
    store = chain[0]

    def interrupt_before_write(*_args, **_kwargs):
        raise SystemExit("simulated interruption")

    monkeypatch.setattr(live_apply_writer, "_mutate_operation", interrupt_before_write)
    try:
        with pytest.raises(SystemExit):
            apply_live_operation(store, *chain[1:], operation_index=0)
        with pytest.raises(PostApplyActivationError, match="not complete"):
            authorize_post_apply_activation(store, *chain[1:])
        assert load_post_apply_activation_authorization(store, chain[4].deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_persisted_authorization_tampering_fails_closed(tmp_path: Path, monkeypatch) -> None:
    chain = _chain(tmp_path, monkeypatch)
    store = chain[0]
    try:
        apply_live_operation(store, *chain[1:], operation_index=0)
        authorize_post_apply_activation(store, *chain[1:])
        store._connection.execute(
            "UPDATE post_apply_activation_authorization SET candidate_sha = ? "
            "WHERE deployment_id = ?",
            ("e" * 40, chain[4].deployment_id),
        )
        store._connection.commit()
        with pytest.raises(PostApplyActivationError, match="state is invalid"):
            load_post_apply_activation_authorization(store, chain[4].deployment_id)
    finally:
        store.__exit__(None, None, None)


def test_activation_authorization_is_not_constructible_from_scalars() -> None:
    assert tuple(inspect.signature(PostApplyActivationAuthorization).parameters) == ()
    with pytest.raises(TypeError):
        PostApplyActivationAuthorization(  # type: ignore[call-arg]
            deployment_id="forged",
            target="owner/private-repo",
        )


def test_stage_verification_failure_is_sanitized(tmp_path: Path, monkeypatch) -> None:
    chain = _chain(tmp_path, monkeypatch)
    store = chain[0]
    try:
        apply_live_operation(store, *chain[1:], operation_index=0)

        def fail_with_private_detail(_stage):
            raise RuntimeError("SECRET /homeassistant/private.yaml")

        monkeypatch.setattr(
            "ha_syncapp.post_apply_activation.verify_candidate_stage",
            fail_with_private_detail,
        )
        with pytest.raises(PostApplyActivationError, match="evidence is invalid") as caught:
            authorize_post_apply_activation(store, *chain[1:])
        assert "SECRET" not in str(caught.value)
        assert "private.yaml" not in str(caught.value)
    finally:
        store.__exit__(None, None, None)


def test_activation_discovery_is_bounded(tmp_path: Path, monkeypatch) -> None:
    chain = _chain(tmp_path, monkeypatch)
    store = chain[0]
    try:
        apply_live_operation(store, *chain[1:], operation_index=0)
        authorize_post_apply_activation(store, *chain[1:])
        monkeypatch.setattr(
            "ha_syncapp.post_apply_activation._MAX_DISCOVERABLE_AUTHORIZATIONS",
            0,
        )
        with pytest.raises(PostApplyActivationError, match="exceeds the limit"):
            discover_post_apply_activation_authorizations(store)
    finally:
        store.__exit__(None, None, None)
