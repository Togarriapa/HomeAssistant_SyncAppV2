from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp.candidate_dependency_execution import (
    CandidateDependencyExecutionError,
    candidate_dependency_runtime_evidence,
    execute_candidate_dependency_once,
    load_candidate_dependency_checkpoint,
)
from ha_syncapp.candidate_integrity_execution import execute_candidate_integrity_once
from ha_syncapp.runtime_inventory import RuntimeInventoryInput
from test_candidate_integrity_execution import NOW, TOKEN, _changes, _integrity, _ready


def _integrity_ready(tmp_path):
    store, staged, workspace, staging, home, observe, fetch = _ready(tmp_path)
    result = execute_candidate_integrity_once(
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
    return store, result, staging, home


def _runtime() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={"source": "candidate_dependency"},
        homeassistant={"entities": [], "services": []},
    )


def test_plans_before_runtime_collection_then_completes_and_advances(tmp_path) -> None:
    store, integrity_result, staging, home = _integrity_ready(tmp_path)

    def collect(*, token):
        assert token == TOKEN
        checkpoint = load_candidate_dependency_checkpoint(
            store, integrity_result.orchestration.candidate_sha
        )
        assert checkpoint is not None and checkpoint.phase == "planned"
        return _runtime()

    try:
        result = execute_candidate_dependency_once(
            store,
            integrity_result.orchestration,
            core_token=TOKEN,
            staging_root=staging,
            home_assistant_root=home,
            runtime_collector=collect,
            now=NOW + timedelta(seconds=1),
        )
        assert not result.replayed
        assert result.checkpoint.phase == "completed"
        assert result.orchestration.phase == "dependencies_analyzed"
        assert result.orchestration.next_action == "classify_risk"
    finally:
        store.__exit__(None, None, None)


def test_completed_replay_uses_no_credentials_or_runtime_transport(tmp_path) -> None:
    store, integrity_result, staging, home = _integrity_ready(tmp_path)
    try:
        first = execute_candidate_dependency_once(
            store,
            integrity_result.orchestration,
            core_token=TOKEN,
            staging_root=staging,
            home_assistant_root=home,
            runtime_collector=lambda *, token: _runtime(),
            now=NOW + timedelta(seconds=1),
        )

        def forbidden(*args, **kwargs):
            pytest.fail("completed replay must not use credentials or transport")

        replay = execute_candidate_dependency_once(
            store,
            first.orchestration,
            core_token=None,
            staging_root=staging,
            home_assistant_root=home,
            runtime_collector=forbidden,
            analyzer=forbidden,
            now=NOW + timedelta(seconds=2),
        )
        assert replay.replayed
        assert replay.checkpoint == first.checkpoint
        evidence = candidate_dependency_runtime_evidence(store)
        assert len(evidence) == 1 and evidence[0].phase == "completed"
        assert integrity_result.orchestration.candidate_sha not in repr(evidence)
    finally:
        store.__exit__(None, None, None)


def test_missing_core_credentials_remains_planned_and_retryable(tmp_path) -> None:
    store, integrity_result, staging, home = _integrity_ready(tmp_path)
    try:
        with pytest.raises(CandidateDependencyExecutionError, match="credentials") as error:
            execute_candidate_dependency_once(
                store,
                integrity_result.orchestration,
                core_token=None,
                staging_root=staging,
                home_assistant_root=home,
                now=NOW + timedelta(seconds=1),
            )
        assert error.value.transient
        checkpoint = load_candidate_dependency_checkpoint(
            store, integrity_result.orchestration.candidate_sha
        )
        assert checkpoint is not None and checkpoint.phase == "planned"
    finally:
        store.__exit__(None, None, None)


def test_stale_concurrent_authority_cannot_reexecute_completed_analysis(tmp_path) -> None:
    store, integrity_result, staging, home = _integrity_ready(tmp_path)
    try:
        execute_candidate_dependency_once(
            store,
            integrity_result.orchestration,
            core_token=TOKEN,
            staging_root=staging,
            home_assistant_root=home,
            runtime_collector=lambda *, token: _runtime(),
            now=NOW + timedelta(seconds=1),
        )

        with pytest.raises(CandidateDependencyExecutionError, match="evidence is invalid"):
            execute_candidate_dependency_once(
                store,
                integrity_result.orchestration,
                core_token=TOKEN,
                staging_root=staging,
                home_assistant_root=home,
                runtime_collector=lambda **kwargs: pytest.fail(
                    "stale authority must fail before transport"
                ),
                now=NOW + timedelta(seconds=2),
            )
    finally:
        store.__exit__(None, None, None)
