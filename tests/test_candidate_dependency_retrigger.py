from __future__ import annotations

from datetime import timedelta

from ha_syncapp.candidate_dependency_execution import execute_candidate_dependency_once
from ha_syncapp.candidate_dependency_retrigger import run_candidate_dependency_retrigger_pass
from ha_syncapp.candidate_integrity_execution import execute_candidate_integrity_once
from ha_syncapp.runtime_inventory import RuntimeInventoryInput
from test_candidate_integrity_execution import NOW, TOKEN, _changes, _integrity, _ready


def test_retrigger_claims_and_completes_at_most_one_dependency_action(tmp_path) -> None:
    store, staged, workspace, staging, home, observe, fetch = _ready(tmp_path)
    try:
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
        running = store._get_work("candidate", integrity.orchestration.candidate_sha)
        store.defer_work(running, now=NOW)

        def execute(store, orchestration, **kwargs):
            return execute_candidate_dependency_once(
                store,
                orchestration,
                core_token=kwargs["core_token"],
                staging_root=kwargs["staging_root"],
                home_assistant_root=kwargs["home_assistant_root"],
                runtime_collector=lambda *, token: RuntimeInventoryInput(
                    manifest={}, homeassistant={"entities": [], "services": []}
                ),
                now=kwargs["now"],
            )

        result = run_candidate_dependency_retrigger_pass(
            store,
            home,
            staging,
            TOKEN,
            reference_time=NOW + timedelta(seconds=60),
            executor=execute,
        )
        assert result.considered == 1
        assert result.processed == "completed"

        replay = run_candidate_dependency_retrigger_pass(
            store,
            home,
            staging,
            TOKEN,
            reference_time=NOW + timedelta(seconds=120),
            executor=execute,
        )
        assert replay.processed is None
    finally:
        store.__exit__(None, None, None)
