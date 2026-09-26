from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp import candidate_stage as stage_module
from ha_syncapp.candidate_detection import CandidateObservation
from ha_syncapp.candidate_fetch import CandidateFetch, CandidateFetchError
from ha_syncapp.candidate_fetch_stage_execution import (
    CandidateFetchStageExecutionError,
    candidate_fetch_stage_runtime_evidence,
    execute_candidate_fetch_stage_once,
    load_candidate_fetch_stage_checkpoint,
)
from ha_syncapp.candidate_orchestration import register_claimed_candidate
from ha_syncapp.candidate_stage import CandidateStage
from ha_syncapp.github_repo import RepositoryVerificationError
from ha_syncapp.state import StateStore

NOW = datetime(2026, 9, 26, 16, 0, tzinfo=UTC)
TARGET = "owner/home-assistant-config"
REPOSITORY_ID = 42
CANDIDATE_SHA = "a" * 40
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
    orchestration = register_claimed_candidate(
        store,
        claimed,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        now=NOW,
    )
    return store, orchestration, workspace, staging, home


def _transport(staging: Path):
    calls: list[str] = []

    def observe(target: str, token: str, *, expected_id: int):
        assert (target, token, expected_id) == (TARGET, TOKEN, REPOSITORY_ID)
        calls.append("observe")
        return CandidateObservation(TARGET, REPOSITORY_ID, "candidate", CANDIDATE_SHA)

    def fetch(observation, expected_sha, token, workspace_root, home_root):
        assert observation.commit_sha == expected_sha == CANDIDATE_SHA
        calls.append("fetch")
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
        calls.append("stage")
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

    return calls, observe, fetch, stage


def test_plans_before_fetch_then_checkpoints_and_advances_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, orchestration, workspace, staging, home = _ready(tmp_path)
    calls, observe, fetch, stage = _transport(staging)
    original_fetch = fetch

    def assert_planned(*args, **kwargs):
        checkpoint = load_candidate_fetch_stage_checkpoint(store, CANDIDATE_SHA)
        assert checkpoint is not None and checkpoint.phase == "planned"
        return original_fetch(*args, **kwargs)

    monkeypatch.setattr(
        "ha_syncapp.candidate_fetch_stage_execution.verify_candidate_stage",
        lambda candidate_stage: None,
    )
    try:
        result = execute_candidate_fetch_stage_once(
            store,
            orchestration,
            token=TOKEN,
            workspace_root=workspace,
            staging_root=staging,
            home_assistant_root=home,
            observer=observe,
            fetcher=assert_planned,
            stager=stage,
            now=NOW,
        )
        assert not result.replayed
        assert result.checkpoint.phase == "completed"
        assert result.checkpoint.manifest_sha256 is not None
        assert result.checkpoint.entry_count == 0
        assert result.checkpoint.total_bytes == 0
        assert calls == ["observe", "fetch", "stage"]
        advanced = result.orchestration
        assert advanced.phase == "staged"
        assert advanced.next_action == "analyze"
        assert (staging / f".syncapp-candidate-stage-{result.checkpoint.workspace_id}").is_dir()
    finally:
        store.__exit__(None, None, None)


def test_completed_replay_requires_no_credentials_or_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, orchestration, workspace, staging, home = _ready(tmp_path)
    _, observe, fetch, stage = _transport(staging)
    monkeypatch.setattr(
        "ha_syncapp.candidate_fetch_stage_execution.verify_candidate_stage",
        lambda candidate_stage: None,
    )
    try:
        first = execute_candidate_fetch_stage_once(
            store,
            orchestration,
            token=TOKEN,
            workspace_root=workspace,
            staging_root=staging,
            home_assistant_root=home,
            observer=observe,
            fetcher=fetch,
            stager=stage,
            now=NOW,
        )

        def forbidden(*args, **kwargs):
            pytest.fail("completed replay must not use transport")

        replay = execute_candidate_fetch_stage_once(
            store,
            first.orchestration,
            token=None,
            workspace_root=workspace,
            staging_root=staging,
            home_assistant_root=home,
            observer=forbidden,
            fetcher=forbidden,
            stager=forbidden,
            now=NOW,
        )
        assert replay.replayed
        assert replay.checkpoint == first.checkpoint
        runtime = candidate_fetch_stage_runtime_evidence(store)
        assert len(runtime) == 1
        assert runtime[0].phase == "completed"
        assert CANDIDATE_SHA not in repr(runtime)
        assert TARGET not in repr(runtime)
    finally:
        store.__exit__(None, None, None)


def test_retry_removes_only_owned_abandoned_stage_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, orchestration, workspace, staging, home = _ready(tmp_path)
    orphan = staging / ".git-workspace-candidate-stage-orphan.tmp"
    orphan.mkdir(mode=0o700)
    _, observe, fetch, stage = _transport(staging)
    monkeypatch.setattr(
        "ha_syncapp.candidate_fetch_stage_execution.verify_candidate_stage",
        lambda candidate_stage: None,
    )
    try:
        execute_candidate_fetch_stage_once(
            store,
            orchestration,
            token=TOKEN,
            workspace_root=workspace,
            staging_root=staging,
            home_assistant_root=home,
            observer=observe,
            fetcher=fetch,
            stager=stage,
            now=NOW,
        )
        assert not orphan.exists()
    finally:
        store.__exit__(None, None, None)


def test_completed_replay_fails_closed_when_stage_manifest_is_tampered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, orchestration, workspace, staging, home = _ready(tmp_path)
    _, observe, fetch, stage = _transport(staging)
    monkeypatch.setattr(
        "ha_syncapp.candidate_fetch_stage_execution.verify_candidate_stage",
        lambda candidate_stage: None,
    )
    try:
        first = execute_candidate_fetch_stage_once(
            store,
            orchestration,
            token=TOKEN,
            workspace_root=workspace,
            staging_root=staging,
            home_assistant_root=home,
            observer=observe,
            fetcher=fetch,
            stager=stage,
            now=NOW,
        )
        destination = staging / f".syncapp-candidate-stage-{first.checkpoint.workspace_id}"
        (destination / "manifest.json").write_bytes(b"{}")
        with pytest.raises(CandidateFetchStageExecutionError, match="invalid") as error:
            execute_candidate_fetch_stage_once(
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


def test_transport_failure_is_sanitized_and_retryable(tmp_path: Path) -> None:
    store, orchestration, workspace, staging, home = _ready(tmp_path)
    _, observe, _, _ = _transport(staging)

    def fail(*args, **kwargs):
        raise CandidateFetchError("confined Git command failed: secret-token")

    try:
        with pytest.raises(
            CandidateFetchStageExecutionError, match="temporarily unavailable"
        ) as error:
            execute_candidate_fetch_stage_once(
                store,
                orchestration,
                token=TOKEN,
                workspace_root=workspace,
                staging_root=staging,
                home_assistant_root=home,
                observer=observe,
                fetcher=fail,
                now=NOW,
            )
        assert error.value.transient
        assert TOKEN not in str(error.value)
        checkpoint = load_candidate_fetch_stage_checkpoint(store, CANDIDATE_SHA)
        assert checkpoint is not None and checkpoint.phase == "planned"
    finally:
        store.__exit__(None, None, None)


def test_repository_observation_transport_failure_is_retryable(tmp_path: Path) -> None:
    store, orchestration, workspace, staging, home = _ready(tmp_path)

    def fail(*args: object, **kwargs: object) -> CandidateObservation:
        raise RepositoryVerificationError(
            "GitHub repository verification transport failed: secret-token"
        )

    try:
        with pytest.raises(
            CandidateFetchStageExecutionError, match="temporarily unavailable"
        ) as error:
            execute_candidate_fetch_stage_once(
                store,
                orchestration,
                token=TOKEN,
                workspace_root=workspace,
                staging_root=staging,
                home_assistant_root=home,
                observer=fail,
                now=NOW,
            )
        assert error.value.transient
        assert TOKEN not in str(error.value)
    finally:
        store.__exit__(None, None, None)
