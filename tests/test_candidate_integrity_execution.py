from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp import candidate_stage as stage_module
from ha_syncapp.candidate_changes import CandidateChangeError, CandidateChanges
from ha_syncapp.candidate_detection import CandidateObservation
from ha_syncapp.candidate_fetch import CandidateFetch
from ha_syncapp.candidate_fetch_stage_execution import execute_candidate_fetch_stage_once
from ha_syncapp.candidate_integrity import CandidateIntegrity
from ha_syncapp.candidate_integrity_execution import (
    CandidateIntegrityExecutionError,
    candidate_integrity_runtime_evidence,
    execute_candidate_integrity_once,
    load_candidate_integrity_checkpoint,
)
from ha_syncapp.candidate_orchestration import register_claimed_candidate
from ha_syncapp.candidate_stage import CandidateStage
from ha_syncapp.github_repo import RepositoryVerificationError
from ha_syncapp.state import StateStore

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
TARGET = "owner/home-assistant-config"
REPOSITORY_ID = 42
CANDIDATE_SHA = "a" * 40
BASELINE_SHA = "b" * 40
TOKEN = "secret-token"


def _ready(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "workspace"
    staging = tmp_path / "staging"
    home = tmp_path / "homeassistant"
    for root in (workspace, staging, home):
        root.mkdir(mode=0o700)
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    store.enqueue_work("candidate", CANDIDATE_SHA, now=NOW)
    claimed = store.claim_work_kind("candidate", now=NOW)
    assert claimed is not None
    detected = register_claimed_candidate(
        store, claimed, target=TARGET, repository_id=REPOSITORY_ID, now=NOW
    )

    def observe(target: str, token: str, *, expected_id: int):
        assert (target, token, expected_id) == (TARGET, TOKEN, REPOSITORY_ID)
        return CandidateObservation(TARGET, REPOSITORY_ID, "candidate", CANDIDATE_SHA)

    def fetch(observation, expected_sha, token, workspace_root, home_root):
        root = workspace_root / "fetch-result"
        root.mkdir()
        return CandidateFetch(
            root,
            TARGET,
            REPOSITORY_ID,
            "candidate",
            CANDIDATE_SHA,
            "refs/syncapp/candidate-fetch",
        )

    def stage(fetched, staging_root, home_root):
        root = staging_root / ".git-workspace-candidate-stage-test.tmp"
        tree = root / "tree"
        tree.mkdir(parents=True)
        root.chmod(0o700)
        tree.chmod(0o700)
        manifest = root / "manifest.json"
        manifest_bytes = stage_module._manifest_bytes(
            target=TARGET,
            repository_id=REPOSITORY_ID,
            branch="candidate",
            commit_sha=CANDIDATE_SHA,
            entries=(),
        )
        manifest.write_bytes(manifest_bytes)
        manifest.chmod(0o600)
        return CandidateStage(
            root=root,
            tree=tree,
            manifest=manifest,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            target=TARGET,
            repository_id=REPOSITORY_ID,
            branch="candidate",
            commit_sha=CANDIDATE_SHA,
            entries=(),
        )

    staged = execute_candidate_fetch_stage_once(
        store,
        detected,
        token=TOKEN,
        workspace_root=workspace,
        staging_root=staging,
        home_assistant_root=home,
        observer=observe,
        fetcher=fetch,
        stager=stage,
        now=NOW,
    ).orchestration
    return store, staged, workspace, staging, home, observe, fetch


def _changes() -> CandidateChanges:
    return CandidateChanges(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        changes=(),
    )


def _integrity(stage: CandidateStage) -> CandidateIntegrity:
    return CandidateIntegrity(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        stage_manifest_sha256=stage.manifest_sha256,
        changed_paths=(),
    )


def test_plans_before_network_then_checkpoints_and_advances(tmp_path: Path) -> None:
    store, staged, workspace, staging, home, observe, fetch = _ready(tmp_path)

    def assert_planned(fetched: CandidateFetch, stage: CandidateStage, token: str):
        checkpoint = load_candidate_integrity_checkpoint(store, CANDIDATE_SHA)
        assert checkpoint is not None and checkpoint.phase == "planned"
        return _changes()

    try:
        result = execute_candidate_integrity_once(
            store,
            staged,
            token=TOKEN,
            workspace_root=workspace,
            staging_root=staging,
            home_assistant_root=home,
            observer=observe,
            fetcher=fetch,
            change_detector=assert_planned,
            integrity_validator=lambda fetched, stage, changes: _integrity(stage),
            now=NOW,
        )
        assert not result.replayed
        assert result.checkpoint.phase == "completed"
        assert result.checkpoint.changed_count == 0
        assert result.orchestration.phase == "integrity_verified"
        assert result.orchestration.next_action == "analyze_dependencies"
    finally:
        store.__exit__(None, None, None)


def test_completed_replay_is_credential_and_network_free(tmp_path: Path) -> None:
    store, staged, workspace, staging, home, observe, fetch = _ready(tmp_path)
    try:
        first = execute_candidate_integrity_once(
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

        def forbidden(*args, **kwargs):
            pytest.fail("completed replay must not use credentials or transport")

        replay = execute_candidate_integrity_once(
            store,
            first.orchestration,
            token=None,
            workspace_root=workspace,
            staging_root=staging,
            home_assistant_root=home,
            observer=forbidden,
            fetcher=forbidden,
            change_detector=forbidden,
            integrity_validator=forbidden,
            now=NOW,
        )
        assert replay.replayed
        assert replay.checkpoint == first.checkpoint
        runtime = candidate_integrity_runtime_evidence(store)
        assert len(runtime) == 1 and runtime[0].phase == "completed"
        assert TARGET not in repr(runtime)
        assert CANDIDATE_SHA not in repr(runtime)
    finally:
        store.__exit__(None, None, None)


def test_transient_baseline_failure_remains_planned_and_is_sanitized(tmp_path: Path) -> None:
    store, staged, workspace, staging, home, observe, fetch = _ready(tmp_path)

    def fail(fetched: CandidateFetch, stage: CandidateStage, token: str):
        outer = CandidateChangeError("trusted Repo B main could not be established")
        outer.__cause__ = RepositoryVerificationError(
            "GitHub repository verification transport failed: secret-token"
        )
        raise outer

    try:
        with pytest.raises(
            CandidateIntegrityExecutionError, match="temporarily unavailable"
        ) as error:
            execute_candidate_integrity_once(
                store,
                staged,
                token=TOKEN,
                workspace_root=workspace,
                staging_root=staging,
                home_assistant_root=home,
                observer=observe,
                fetcher=fetch,
                change_detector=fail,
                now=NOW,
            )
        assert error.value.transient
        assert TOKEN not in str(error.value)
        checkpoint = load_candidate_integrity_checkpoint(store, CANDIDATE_SHA)
        assert checkpoint is not None and checkpoint.phase == "planned"
    finally:
        store.__exit__(None, None, None)


def test_tampered_completed_changes_fail_closed_without_network(tmp_path: Path) -> None:
    store, staged, workspace, staging, home, observe, fetch = _ready(tmp_path)
    try:
        first = execute_candidate_integrity_once(
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
        store._connection.execute(
            "UPDATE candidate_integrity_checkpoint SET changes_json = '[{}]' "
            "WHERE candidate_sha = ?",
            (CANDIDATE_SHA,),
        )
        store._connection.commit()
        with pytest.raises(CandidateIntegrityExecutionError, match="invalid") as error:
            execute_candidate_integrity_once(
                store,
                first.orchestration,
                token=None,
                workspace_root=workspace,
                staging_root=staging,
                home_assistant_root=home,
                now=NOW,
            )
        assert not error.value.transient
    finally:
        store.__exit__(None, None, None)
