from pathlib import Path

import pytest
from ha_syncapp import runtime_sync_retrigger, runtime_sync_work
from ha_syncapp.core_runtime_bundle import CoreRuntimeBundleError
from ha_syncapp.runtime_inventory import RuntimeInventoryInput
from ha_syncapp.runtime_sync import RuntimeSyncDisposition, RuntimeSyncResult
from ha_syncapp.runtime_sync_work import RuntimeSyncWorkResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
COMMIT_SHA = "1" * 40
ARTIFACT_ID = "b" * 64
SNAPSHOT_ID = "a" * 64


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    store = StateStore(data)
    store.__enter__()
    return store


def _inventory() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={
            "home_assistant_version": "2026.9.1",
            "registry_entity_count": 1,
            "registry_device_count": 1,
            "area_count": 1,
        },
        homeassistant={
            "states": [],
            "services": [],
            "entities": [{"entity_id": "light.one"}],
            "devices": [{"id": "device-one"}],
            "areas": [{"id": "kitchen"}],
        },
    )


def _sync_result(disposition: RuntimeSyncDisposition) -> RuntimeSyncResult:
    return RuntimeSyncResult(
        disposition=disposition,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="runtime",
        artifact_id=ARTIFACT_ID,
        snapshot_id=SNAPSHOT_ID,
        commit_sha=COMMIT_SHA,
        baseline=None,
    )


def _run(
    store: StateStore,
    tmp_path: Path,
    *,
    target: str = TARGET,
    github_token: str = "github-token",
    core_token: str = "core-token",
) -> runtime_sync_retrigger.RuntimeSyncRetriggerResult:
    return runtime_sync_retrigger.run_runtime_sync_retrigger_pass(
        store,
        tmp_path / "runtime-staging",
        tmp_path / "snapshots",
        tmp_path / "workspaces",
        target,
        github_token,
        core_token=core_token,
    )


def test_idle_pass_does_not_collect_or_consume_other_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.enqueue_work("candidate", "candidate-sha")
    collected = False

    def collect(*args: object, **kwargs: object) -> RuntimeInventoryInput:
        nonlocal collected
        collected = True
        return _inventory()

    monkeypatch.setattr(runtime_sync_retrigger, "collect_core_runtime_bundle", collect)
    try:
        result = _run(store, tmp_path)
        unrelated = store.claim_work()
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 0
    assert result.processed is None
    assert collected is False
    assert unrelated is not None and unrelated.work_kind == "candidate"


def test_valid_claim_collects_combined_core_data_then_executes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    events: list[str] = []
    inventory = _inventory()

    def collect(*, token: str | None = None) -> RuntimeInventoryInput:
        assert token == "core-token"
        events.append("collect")
        return inventory

    def execute(
        state: StateStore,
        item,
        supplied_inventory: RuntimeInventoryInput,
        *args: object,
    ) -> RuntimeSyncWorkResult:
        assert supplied_inventory is inventory
        assert supplied_inventory.homeassistant["entities"] == [{"entity_id": "light.one"}]
        assert supplied_inventory.homeassistant["devices"] == [{"id": "device-one"}]
        assert supplied_inventory.homeassistant["areas"] == [{"id": "kitchen"}]
        events.append("execute")
        completed = state.complete_work(item)
        return RuntimeSyncWorkResult(
            completed,
            _sync_result(RuntimeSyncDisposition.NO_CHANGE),
        )

    monkeypatch.setattr(runtime_sync_retrigger, "collect_core_runtime_bundle", collect)
    monkeypatch.setattr(runtime_sync_retrigger, "execute_claimed_runtime_sync_work", execute)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert events == ["collect", "execute"]
    assert result.processed is not None
    assert result.processed.work.status == "succeeded"


def test_core_bundle_failure_moves_claim_to_retry_without_leaking_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    core_secret = "supervisor-core-secret"
    github_secret = "github-super-secret"

    def fail(*, token: str | None = None) -> RuntimeInventoryInput:
        raise CoreRuntimeBundleError(f"collection failed with {token}")

    monkeypatch.setattr(runtime_sync_retrigger, "collect_core_runtime_bundle", fail)
    try:
        result = _run(
            store,
            tmp_path,
            github_token=github_secret,
            core_token=core_secret,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == "retry"
    assert result.processed.work.next_attempt_at is not None
    assert result.processed.synchronization is None
    assert core_secret not in repr(result)
    assert github_secret not in repr(result)


def test_mismatched_target_blocks_before_collection_or_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    called = False

    def collect(*args: object, **kwargs: object) -> RuntimeInventoryInput:
        nonlocal called
        called = True
        return _inventory()

    monkeypatch.setattr(runtime_sync_retrigger, "collect_core_runtime_bundle", collect)
    try:
        result = _run(store, tmp_path, target="Owner/Another-Home")
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == "blocked"
    assert result.processed.work.next_attempt_at is None
    assert called is False


def test_retrigger_recovers_interrupted_runtime_work_before_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    claimed = runtime_sync_work.claim_runtime_sync_work(store)
    assert claimed is not None and claimed.status == "running"

    monkeypatch.setattr(
        runtime_sync_retrigger,
        "collect_core_runtime_bundle",
        lambda **kwargs: _inventory(),
    )

    def execute(state: StateStore, item, *args: object) -> RuntimeSyncWorkResult:
        assert item.status == "running"
        assert item.attempts == 2
        completed = state.complete_work(item)
        return RuntimeSyncWorkResult(
            completed,
            _sync_result(RuntimeSyncDisposition.NO_CHANGE),
        )

    monkeypatch.setattr(runtime_sync_retrigger, "execute_claimed_runtime_sync_work", execute)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 1
    assert result.processed is not None
    assert result.processed.work.attempts == 2
    assert result.processed.work.status == "succeeded"


def test_execution_error_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    secret = "github-super-secret"
    monkeypatch.setattr(
        runtime_sync_retrigger,
        "collect_core_runtime_bundle",
        lambda **kwargs: _inventory(),
    )

    def fail(*args: object, **kwargs: object) -> RuntimeSyncWorkResult:
        raise runtime_sync_work.RuntimeSyncWorkError(secret)

    monkeypatch.setattr(runtime_sync_retrigger, "execute_claimed_runtime_sync_work", fail)
    try:
        with pytest.raises(runtime_sync_retrigger.RuntimeSyncRetriggerError) as caught:
            _run(store, tmp_path, github_token=secret)
    finally:
        store.__exit__(None, None, None)

    assert secret not in str(caught.value)


def test_unopened_state_store_fails_closed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)

    with pytest.raises(runtime_sync_retrigger.RuntimeSyncRetriggerError, match="failed closed"):
        _run(store, tmp_path)
