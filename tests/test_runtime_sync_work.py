from pathlib import Path

import pytest
from ha_syncapp import runtime_sync_work
from ha_syncapp.runtime_inventory import RuntimeInventoryInput
from ha_syncapp.runtime_sync import RuntimeSyncDisposition, RuntimeSyncError, RuntimeSyncResult
from ha_syncapp.state import StateStore, WorkItem

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
COMMIT_SHA = "1" * 40
ARTIFACT_ID = "b" * 64
SNAPSHOT_ID = "a" * 64


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _inventory() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={"home_assistant_version": "2026.9.1"},
        homeassistant={"states": [], "services": []},
    )


def _result(disposition: RuntimeSyncDisposition) -> RuntimeSyncResult:
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


def _execute(
    store: StateStore,
    item: WorkItem,
    tmp_path: Path,
    *,
    target: str = TARGET,
) -> runtime_sync_work.RuntimeSyncWorkResult:
    return runtime_sync_work.execute_claimed_runtime_sync_work(
        store,
        item,
        _inventory(),
        tmp_path / "runtime-staging",
        tmp_path / "snapshots",
        tmp_path / "workspaces",
        target,
        "secret-token",
    )


def test_enqueue_runtime_work_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        first = runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
        second = runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    finally:
        store.__exit__(None, None, None)

    assert first.work_kind == "runtime"
    assert second.work_key == first.work_key
    assert second.status == "pending"


def test_runtime_claim_does_not_consume_other_work_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue_work("candidate", "candidate-a")
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    try:
        claimed = runtime_sync_work.claim_runtime_sync_work(store)
        unrelated = store.claim_work()
    finally:
        store.__exit__(None, None, None)

    assert claimed is not None and claimed.work_kind == "runtime"
    assert unrelated is not None and unrelated.work_kind == "candidate"


@pytest.mark.parametrize(
    "disposition",
    [
        RuntimeSyncDisposition.INITIALIZED,
        RuntimeSyncDisposition.PUBLISHED,
        RuntimeSyncDisposition.NO_CHANGE,
    ],
)
def test_successful_runtime_outcomes_complete_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: RuntimeSyncDisposition,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    item = runtime_sync_work.claim_runtime_sync_work(store)
    assert item is not None

    def synchronize(*args: object, **kwargs: object) -> RuntimeSyncResult:
        return _result(disposition)

    monkeypatch.setattr(runtime_sync_work, "synchronize_runtime_inventory", synchronize)
    try:
        result = _execute(store, item, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "succeeded"
    assert result.synchronization is not None
    assert result.synchronization.disposition is disposition


@pytest.mark.parametrize(
    "disposition",
    [
        RuntimeSyncDisposition.BASELINE_REQUIRED,
        RuntimeSyncDisposition.DIVERGED,
        RuntimeSyncDisposition.REMOTE_MISSING,
    ],
)
def test_deterministic_runtime_refusals_block_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: RuntimeSyncDisposition,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    item = runtime_sync_work.claim_runtime_sync_work(store)
    assert item is not None

    def synchronize(*args: object, **kwargs: object) -> RuntimeSyncResult:
        return _result(disposition)

    monkeypatch.setattr(runtime_sync_work, "synchronize_runtime_inventory", synchronize)
    try:
        result = _execute(store, item, tmp_path)
        assert runtime_sync_work.claim_runtime_sync_work(store) is None
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "blocked"
    assert result.work.next_attempt_at is None


def test_guarded_runtime_failure_uses_retry_without_leaking_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    item = runtime_sync_work.claim_runtime_sync_work(store)
    assert item is not None
    secret = "ghp_super_secret_value"

    def fail(*args: object, **kwargs: object) -> RuntimeSyncResult:
        raise RuntimeSyncError(secret)

    monkeypatch.setattr(runtime_sync_work, "synchronize_runtime_inventory", fail)
    try:
        result = runtime_sync_work.execute_claimed_runtime_sync_work(
            store,
            item,
            _inventory(),
            tmp_path / "runtime-staging",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            secret,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "retry"
    assert result.synchronization is None
    assert secret not in repr(result)


def test_claim_identity_must_match_explicit_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    item = runtime_sync_work.claim_runtime_sync_work(store)
    assert item is not None
    try:
        with pytest.raises(runtime_sync_work.RuntimeSyncWorkError, match="identity does not match"):
            _execute(store, item, tmp_path, target="Owner/Another-Home")
    finally:
        store.__exit__(None, None, None)


def test_invalid_inventory_is_rejected_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_sync_work.enqueue_runtime_sync_work(store, TARGET)
    item = runtime_sync_work.claim_runtime_sync_work(store)
    assert item is not None
    called = False

    def synchronize(*args: object, **kwargs: object) -> RuntimeSyncResult:
        nonlocal called
        called = True
        return _result(RuntimeSyncDisposition.PUBLISHED)

    monkeypatch.setattr(runtime_sync_work, "synchronize_runtime_inventory", synchronize)
    try:
        with pytest.raises(runtime_sync_work.RuntimeSyncWorkError, match="inventory evidence"):
            runtime_sync_work.execute_claimed_runtime_sync_work(
                store,
                item,
                object(),  # type: ignore[arg-type]
                tmp_path / "runtime-staging",
                tmp_path / "snapshots",
                tmp_path / "workspaces",
                TARGET,
                "secret-token",
            )
    finally:
        store.__exit__(None, None, None)

    assert called is False


@pytest.mark.parametrize("target", ["", " Owner/Private-Home", "Owner/Private-Home "])
def test_work_key_rejects_invalid_target(target: str) -> None:
    with pytest.raises(runtime_sync_work.RuntimeSyncWorkError, match="identity is invalid"):
        runtime_sync_work.runtime_sync_work_key(target)
