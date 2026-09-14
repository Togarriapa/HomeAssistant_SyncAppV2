from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ha_syncapp.apply_authorization import ApplyAuthorization
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.candidate_stage import CandidateStage, CandidateStageEntry
from ha_syncapp.live_apply_intent_store import record_live_apply_intent
from ha_syncapp.live_apply_plan import LiveApplyOperation, LiveApplyPlan
from ha_syncapp.live_apply_preconditions import (
    LiveApplyPreconditionEvidence,
    prove_live_apply_preconditions,
)
from ha_syncapp.live_apply_progress_store import discover_live_apply_progress
from ha_syncapp.live_apply_writer import LiveApplyWriterError, apply_live_operation
from ha_syncapp.prepared_deployment import PreparedDeployment
from ha_syncapp.stage_prewrite_reproof import StagePrewriteEvidence
from ha_syncapp.state import StateStore


def _blob_id(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def _operation(
    path: str,
    status: str,
    baseline: bytes | None,
    candidate: bytes | None,
) -> LiveApplyOperation:
    baseline_mode = None if baseline is None else "100644"
    baseline_id = None if baseline is None else _blob_id(baseline)
    candidate_mode = None if candidate is None else "100644"
    candidate_id = None if candidate is None else _blob_id(candidate)
    if status == "mode_changed":
        assert baseline is not None and candidate is not None
        candidate_mode = "100755"
        candidate_id = baseline_id
    return LiveApplyOperation(
        path=path,
        status=status,
        baseline_mode=baseline_mode,
        baseline_object_id=baseline_id,
        candidate_mode=candidate_mode,
        candidate_object_id=candidate_id,
        staged_size=None if candidate is None else len(candidate),
        staged_sha256=None if candidate is None else hashlib.sha256(candidate).hexdigest(),
    )


def _chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "modified",
    baseline: bytes | None = b"baseline\n",
    candidate: bytes | None = b"candidate\n",
) -> tuple[
    StateStore,
    ApplyAuthorization,
    StagePrewriteEvidence,
    CandidateStage,
    LiveApplyPlan,
    LiveApplyPreconditionEvidence,
]:
    live = tmp_path / "homeassistant"
    live.mkdir()
    path = "automations.yaml"
    if baseline is not None:
        (live / path).write_bytes(baseline)
        os.chmod(live / path, 0o644)

    operation = _operation(path, status, baseline, candidate)
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
    entries: tuple[CandidateStageEntry, ...]
    if candidate is None:
        entries = ()
    else:
        staged = tree / path
        staged.write_bytes(candidate)
        os.chmod(staged, 0o700 if operation.candidate_mode == "100755" else 0o600)
        entries = (
            CandidateStageEntry(
                path=path,
                git_mode=operation.candidate_mode or "100644",
                object_id=operation.candidate_object_id or "0" * 40,
                size=len(candidate),
                sha256=hashlib.sha256(candidate).hexdigest(),
            ),
        )
    stage = CandidateStage(
        root=stage_root,
        tree=tree,
        manifest=stage_root / "manifest.json",
        manifest_sha256=backup.stage_manifest_sha256,
        target=backup.target,
        repository_id=backup.repository_id,
        branch="candidate",
        commit_sha=backup.candidate_sha,
        entries=entries,
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
        "operations": (operation,),
    }.items():
        object.__setattr__(plan, name, value)

    preconditions = prove_live_apply_preconditions(plan, live)
    store = StateStore(tmp_path / "state")
    store.bind_repository(backup.target, backup.repository_id)
    store.record_prepared_deployment(prepared.deployment_id, backup)
    record_live_apply_intent(store, authorization, stage_evidence, plan, preconditions)
    monkeypatch.setattr("ha_syncapp.live_apply_writer.verify_candidate_stage", lambda _stage: None)
    return store, authorization, stage_evidence, stage, plan, preconditions


def test_modified_file_is_journaled_then_atomically_verified(tmp_path: Path, monkeypatch) -> None:
    store, authorization, stage_evidence, stage, plan, preconditions = _chain(tmp_path, monkeypatch)
    live = Path(preconditions.root)
    try:
        result = apply_live_operation(
            store,
            authorization,
            stage_evidence,
            stage,
            plan,
            preconditions,
            operation_index=0,
        )
        assert result.status == "mutation_verified"
        assert (live / "automations.yaml").read_bytes() == b"candidate\n"
        progress = discover_live_apply_progress(store, plan.deployment_id)
        assert [item.phase for item in progress] == ["mutation_verified"]
    finally:
        store.close()


def test_progress_persistence_failure_happens_before_any_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    store, authorization, stage_evidence, stage, plan, preconditions = _chain(tmp_path, monkeypatch)
    live = Path(preconditions.root)

    def fail_record(*_args, **_kwargs):
        raise RuntimeError("state unavailable")

    monkeypatch.setattr("ha_syncapp.live_apply_writer.record_live_apply_progress", fail_record)
    try:
        with pytest.raises(LiveApplyWriterError, match="progress"):
            apply_live_operation(
                store,
                authorization,
                stage_evidence,
                stage,
                plan,
                preconditions,
                operation_index=0,
            )
        assert (live / "automations.yaml").read_bytes() == b"baseline\n"
    finally:
        store.close()


def test_crash_after_journal_before_write_recovers_as_uncertain(
    tmp_path: Path, monkeypatch
) -> None:
    store, authorization, stage_evidence, stage, plan, preconditions = _chain(tmp_path, monkeypatch)
    live = Path(preconditions.root)

    def crash(*_args, **_kwargs):
        raise OSError("simulated crash window")

    monkeypatch.setattr("ha_syncapp.live_apply_writer._mutate_operation", crash)
    try:
        with pytest.raises(LiveApplyWriterError, match="uncertain"):
            apply_live_operation(
                store,
                authorization,
                stage_evidence,
                stage,
                plan,
                preconditions,
                operation_index=0,
            )
        assert (live / "automations.yaml").read_bytes() == b"baseline\n"
        progress = discover_live_apply_progress(store, plan.deployment_id)
        assert progress[-1].phase == "mutation_started"
        with pytest.raises(LiveApplyWriterError, match="reconciliation"):
            apply_live_operation(
                store,
                authorization,
                stage_evidence,
                stage,
                plan,
                preconditions,
                operation_index=0,
            )
    finally:
        store.close()


def test_stage_tamper_fails_before_journal_or_live_mutation(tmp_path: Path, monkeypatch) -> None:
    store, authorization, stage_evidence, stage, plan, preconditions = _chain(tmp_path, monkeypatch)
    live = Path(preconditions.root)

    def reject(_stage):
        raise RuntimeError("private staged bytes")

    monkeypatch.setattr("ha_syncapp.live_apply_writer.verify_candidate_stage", reject)
    try:
        with pytest.raises(LiveApplyWriterError, match="Stage") as caught:
            apply_live_operation(
                store,
                authorization,
                stage_evidence,
                stage,
                plan,
                preconditions,
                operation_index=0,
            )
        assert "private staged bytes" not in str(caught.value)
        assert (live / "automations.yaml").read_bytes() == b"baseline\n"
        assert discover_live_apply_progress(store, plan.deployment_id) == ()
    finally:
        store.close()


def test_added_deleted_and_mode_only_operations_are_verified(tmp_path: Path, monkeypatch) -> None:
    scenarios = (
        ("added", None, b"candidate\n", True, 0o644),
        ("deleted", b"baseline\n", None, False, None),
        ("mode_changed", b"same\n", b"same\n", True, 0o755),
    )
    for index, (status, baseline, candidate, exists, mode) in enumerate(scenarios):
        case = tmp_path / str(index)
        case.mkdir()
        store, authorization, stage_evidence, stage, plan, preconditions = _chain(
            case,
            monkeypatch,
            status=status,
            baseline=baseline,
            candidate=candidate,
        )
        target = Path(preconditions.root) / "automations.yaml"
        try:
            result = apply_live_operation(
                store,
                authorization,
                stage_evidence,
                stage,
                plan,
                preconditions,
                operation_index=0,
            )
            assert result.status == "mutation_verified"
            assert target.exists() is exists
            if mode is not None:
                assert (target.stat().st_mode & 0o777) == mode
        finally:
            store.close()
