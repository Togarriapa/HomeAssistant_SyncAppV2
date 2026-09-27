from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from ha_syncapp.candidate_backup import CandidateBackupError, CandidateBackupEvidence
from ha_syncapp.candidate_backup_execution import (
    CandidateBackupAuthority,
    CandidateBackupExecutionError,
    execute_candidate_backup_once,
    load_candidate_backup_checkpoint,
)
from ha_syncapp.candidate_semantic_checkpoint import CandidateSemanticCheckpoint
from ha_syncapp.candidate_semantic_execution import (
    execute_candidate_semantic_once,
    load_candidate_semantic_checkpoint,
)
from semantic_fixtures import candidate_inputs
from test_candidate_integrity_execution import NOW
from test_candidate_semantic_execution import _semantic_ready


def _backup_ready(tmp_path, monkeypatch):
    store, orchestration, semantic = _semantic_ready(tmp_path, monkeypatch)
    semantic_result = execute_candidate_semantic_once(
        store,
        orchestration,
        staging_root=tmp_path,
        home_assistant_root=tmp_path,
        validator=lambda *args: semantic,
        now=NOW + timedelta(seconds=4),
    )
    orchestration = semantic_result.orchestration
    inputs_root = tmp_path / "backup-inputs"
    inputs_root.mkdir()
    static, integrity, stage, dependencies, impact, risk, runtime, version = candidate_inputs(
        inputs_root, {"configuration.yaml": b"homeassistant:\n"}
    )
    semantic_checkpoint = load_candidate_semantic_checkpoint(store, orchestration.candidate_sha)
    assert semantic_checkpoint is not None and semantic_checkpoint.completed_at is not None
    semantic = replace(semantic, baseline_sha="b" * 40)
    rebound_plan = CandidateSemanticCheckpoint.plan(
        candidate_sha=semantic_checkpoint.candidate_sha,
        orchestration_sha256=semantic_checkpoint.orchestration_sha256,
        fetch_stage_sha256=semantic_checkpoint.fetch_stage_sha256,
        integrity_sha256=semantic_checkpoint.integrity_sha256,
        dependency_sha256=semantic_checkpoint.dependency_sha256,
        risk_sha256=semantic_checkpoint.risk_sha256,
        static_sha256=semantic_checkpoint.static_sha256,
        target=semantic_checkpoint.target,
        repository_id=semantic_checkpoint.repository_id,
        baseline_sha=semantic.baseline_sha,
        stage_manifest_sha256=semantic_checkpoint.stage_manifest_sha256,
        runtime_sha256=semantic_checkpoint.runtime_sha256,
        core_version=semantic_checkpoint.core_version,
        planned_at=semantic_checkpoint.planned_at,
    )
    semantic_checkpoint = rebound_plan.complete(
        semantic,
        static,
        integrity,
        stage,
        dependencies,
        impact,
        risk,
        runtime,
        version,
        completed_at=semantic_checkpoint.completed_at,
    )
    with store._connection as db:
        db.execute(
            "DELETE FROM candidate_semantic_checkpoint WHERE candidate_sha=?",
            (orchestration.candidate_sha,),
        )
        db.execute(
            "INSERT INTO candidate_semantic_checkpoint VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            semantic_checkpoint.database_values(),
        )
    authority = CandidateBackupAuthority(
        semantic,
        static,
        integrity,
        stage,
        dependencies,
        impact,
        risk,
        runtime,
        version,
        semantic_checkpoint.fetch_stage_sha256,
        semantic_checkpoint.integrity_sha256,
        semantic_checkpoint.dependency_sha256,
        semantic_checkpoint.risk_sha256,
        semantic_checkpoint.static_sha256,
        semantic_checkpoint.record_sha256,
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_backup_execution._load_authority", lambda *args, **kwargs: authority
    )
    return store, orchestration, semantic, authority


def _evidence(semantic, slug="backup-123") -> CandidateBackupEvidence:
    return CandidateBackupEvidence(
        semantic.target,
        semantic.repository_id,
        semantic.baseline_sha,
        semantic.candidate_sha,
        semantic.stage_manifest_sha256,
        semantic.runtime_sha256,
        semantic.risk_level,
        semantic.core_version,
        slug,
    )


def test_success_journals_before_mutation_and_finishes_atomically(tmp_path, monkeypatch) -> None:
    store, orchestration, semantic, _authority = _backup_ready(tmp_path, monkeypatch)
    calls = []

    def create(*args, **kwargs):
        checkpoint = load_candidate_backup_checkpoint(store, orchestration.candidate_sha)
        assert checkpoint is not None and checkpoint.phase == "mutation_started"
        calls.append(kwargs["backup_name"])
        return _evidence(semantic)

    try:
        result = execute_candidate_backup_once(
            store,
            orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            token="secret-token",
            creator=create,
            now=NOW + timedelta(seconds=5),
        )
        assert result.checkpoint.phase == "completed"
        assert result.orchestration.phase == "backup_prepared"
        assert result.orchestration.next_action == "authorize_apply"
        assert result.prepared.evidence == _evidence(semantic)
        assert store.prepared_deployment(result.prepared.deployment_id) == result.prepared
        assert len(calls) == 1
        work = store._get_work("candidate", orchestration.candidate_sha)
        assert work.status == "retry"
        assert work.next_attempt_at == NOW + timedelta(seconds=5)
    finally:
        store.__exit__(None, None, None)


def test_completed_replay_requires_no_credentials_or_transport(tmp_path, monkeypatch) -> None:
    store, orchestration, semantic, _authority = _backup_ready(tmp_path, monkeypatch)
    try:
        first = execute_candidate_backup_once(
            store,
            orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            token="secret-token",
            creator=lambda *args, **kwargs: _evidence(semantic),
            now=NOW + timedelta(seconds=5),
        )
        replay = execute_candidate_backup_once(
            store,
            first.orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            creator=lambda *args, **kwargs: pytest.fail("must not create backup"),
            reconciler=lambda *args, **kwargs: pytest.fail("must not reconcile backup"),
            now=NOW + timedelta(seconds=6),
        )
        assert replay.replayed
        assert replay.prepared == first.prepared
    finally:
        store.__exit__(None, None, None)


def test_uncertain_mutation_never_posts_again_and_reconciles(tmp_path, monkeypatch) -> None:
    store, orchestration, semantic, _authority = _backup_ready(tmp_path, monkeypatch)
    creates = []
    reconciles = []

    def timeout(*args, **kwargs):
        creates.append(kwargs["backup_name"])
        raise CandidateBackupError("sanitized timeout")

    try:
        with pytest.raises(CandidateBackupExecutionError) as caught:
            execute_candidate_backup_once(
                store,
                orchestration,
                staging_root=tmp_path,
                home_assistant_root=tmp_path,
                token="secret-token",
                creator=timeout,
                now=NOW + timedelta(seconds=5),
            )
        assert caught.value.transient
        checkpoint = load_candidate_backup_checkpoint(store, orchestration.candidate_sha)
        assert checkpoint is not None and checkpoint.phase == "uncertain"

        def reconcile(*args, **kwargs):
            reconciles.append(kwargs["request_name"])
            return _evidence(semantic)

        result = execute_candidate_backup_once(
            store,
            orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            token="secret-token",
            creator=lambda *args, **kwargs: pytest.fail("must never POST again"),
            reconciler=reconcile,
            now=NOW + timedelta(seconds=6),
        )
        assert result.checkpoint.phase == "completed"
        assert creates == reconciles
    finally:
        store.__exit__(None, None, None)


def test_failed_uncertain_reconciliation_blocks_exact_candidate(tmp_path, monkeypatch) -> None:
    store, orchestration, _semantic, _authority = _backup_ready(tmp_path, monkeypatch)
    try:
        with pytest.raises(CandidateBackupExecutionError):
            execute_candidate_backup_once(
                store,
                orchestration,
                staging_root=tmp_path,
                home_assistant_root=tmp_path,
                token="secret-token",
                creator=lambda *args, **kwargs: (_ for _ in ()).throw(
                    CandidateBackupError("uncertain")
                ),
                now=NOW + timedelta(seconds=5),
            )
        blocked = execute_candidate_backup_once(
            store,
            orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            token="secret-token",
            creator=lambda *args, **kwargs: pytest.fail("must never POST again"),
            reconciler=lambda *args, **kwargs: (_ for _ in ()).throw(
                CandidateBackupError("missing exact backup")
            ),
            now=NOW + timedelta(seconds=6),
        )
        assert blocked.checkpoint.phase == "blocked"
        assert blocked.orchestration.phase == "blocked"
        assert blocked.prepared is None
        assert store._get_work("candidate", orchestration.candidate_sha).status == "blocked"
    finally:
        store.__exit__(None, None, None)
