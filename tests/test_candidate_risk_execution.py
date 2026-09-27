from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp.candidate_dependencies import CandidateDependencyAnalysis, CandidateDependencyFile
from ha_syncapp.candidate_dependency_checkpoint import CandidateDependencyCheckpoint
from ha_syncapp.candidate_dependency_execution import execute_candidate_dependency_once
from ha_syncapp.candidate_integrity_execution import execute_candidate_integrity_once
from ha_syncapp.candidate_risk_execution import (
    candidate_risk_runtime_evidence,
    execute_candidate_risk_once,
)
from ha_syncapp.runtime_evidence import fingerprint_runtime
from ha_syncapp.runtime_inventory import RuntimeInventoryInput
from test_candidate_integrity_execution import NOW, TOKEN, _changes, _integrity, _ready


def _ready_for_risk(tmp_path):
    store, staged, workspace, staging, home, observe, fetch = _ready(tmp_path)
    integrity = execute_candidate_integrity_once(
        store,
        staged,
        token=TOKEN,
        workspace_root=workspace,
        staging_root=staging,
        home_assistant_root=home,
        observer=observe,
        fetcher=fetch,
        change_detector=lambda fetched, stage, token: _changes(),
        integrity_validator=lambda fetched, stage, changes: _integrity(stage),
        now=NOW,
    )
    runtime = RuntimeInventoryInput(
        manifest={"source": "risk"}, homeassistant={"entities": [], "services": []}
    )
    dependency = execute_candidate_dependency_once(
        store,
        integrity.orchestration,
        core_token=TOKEN,
        staging_root=staging,
        home_assistant_root=home,
        runtime_collector=lambda *, token: runtime,
        now=NOW + timedelta(seconds=1),
    )
    original = dependency.checkpoint
    analysis = CandidateDependencyAnalysis(
        original.target,
        original.repository_id,
        original.baseline_sha,
        original.candidate_sha,
        original.stage_manifest_sha256,
        fingerprint_runtime(runtime),
        "best_effort_lexical",
        (CandidateDependencyFile("automations.yaml", "analyzed_text", (), (), False, ()),),
        (),
        (),
        (),
        (),
        (),
    )
    replacement = CandidateDependencyCheckpoint.plan(
        candidate_sha=original.candidate_sha,
        orchestration_sha256=original.orchestration_sha256,
        fetch_stage_sha256=original.fetch_stage_sha256,
        integrity_sha256=original.integrity_sha256,
        target=original.target,
        repository_id=original.repository_id,
        baseline_sha=original.baseline_sha,
        stage_manifest_sha256=original.stage_manifest_sha256,
        planned_at=original.planned_at,
    ).complete(analysis, runtime, completed_at=original.completed_at)
    with store._connection as db:
        db.execute(
            "DELETE FROM candidate_dependency_checkpoint WHERE candidate_sha = ?",
            (original.candidate_sha,),
        )
        db.execute(
            "INSERT INTO candidate_dependency_checkpoint VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            replacement.database_values(),
        )
    return store, dependency


def test_risk_execution_plans_completes_and_advances_atomically(tmp_path) -> None:
    store, dependency = _ready_for_risk(tmp_path)
    try:
        result = execute_candidate_risk_once(
            store, dependency.orchestration, now=NOW + timedelta(seconds=2)
        )
        assert not result.replayed
        assert result.checkpoint.phase == "completed"
        assert result.orchestration.phase == "risk_classified"
        assert result.orchestration.next_action == "validate"
        assert result.risk.level == "low"
    finally:
        store.__exit__(None, None, None)


def test_completed_risk_replay_is_transport_and_credential_free(tmp_path) -> None:
    store, dependency = _ready_for_risk(tmp_path)
    try:
        first = execute_candidate_risk_once(
            store, dependency.orchestration, now=NOW + timedelta(seconds=2)
        )

        def forbidden(*args, **kwargs):
            pytest.fail("risk replay must not recollect or mutate")

        replay = execute_candidate_risk_once(
            store,
            first.orchestration,
            impact_expander=forbidden,
            classifier=forbidden,
            now=NOW + timedelta(seconds=3),
        )
        assert replay.replayed and replay.risk == first.risk
        evidence = candidate_risk_runtime_evidence(store)
        assert evidence[0].risk_level == "low"
        assert dependency.orchestration.candidate_sha not in repr(evidence)
    finally:
        store.__exit__(None, None, None)
