from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp import local_sync_work
from ha_syncapp.local_sync import LocalSyncDisposition, LocalSyncError, LocalSyncResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
COMMIT_SHA = "1" * 40
SNAPSHOT_ID = "a" * 64


def _open_store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    store = StateStore(data)
    store.__enter__()
    return store


def _claimed_local_sync(store: StateStore):
    local_sync_work.enqueue_local_sync_work(store, TARGET)
    item = local_sync_work.claim_local_sync_work(store)
    assert item is not None
    return item


def _sync_result(disposition: LocalSyncDisposition) -> LocalSyncResult:
    return LocalSyncResult(
        disposition=disposition,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="main",
        snapshot_id=SNAPSHOT_ID,
        commit_sha=COMMIT_SHA,
        baseline=None,
    )


def test_local_sync_enqueue_is_idempotent_and_case_stable(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    try:
        first = local_sync_work.enqueue_local_sync_work(store, TARGET)
        second = local_sync_work.enqueue_local_sync_work(store, TARGET.lower())
    finally:
        store.__exit__(None, None, None)

    assert first == second
    assert first.work_kind == "local_sync"
    assert len(first.work_key) == 64


def test_local_sync_claim_skips_earlier_unrelated_work(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    earlier = datetime(2026, 1, 1, tzinfo=UTC)
    later = earlier + timedelta(seconds=1)
    store.enqueue_work("candidate", "candidate-sha", now=earlier)
    store.enqueue_work(
        "local_sync",
        local_sync_work.local_sync_work_key(TARGET),
        now=later,
    )

    try:
        claimed = local_sync_work.claim_local_sync_work(store, now=later)
        unrelated = store.claim_work(now=later)
    finally:
        store.__exit__(None, None, None)

    assert claimed is not None and claimed.work_kind == "local_sync"
    assert claimed.attempts == 1 and claimed.status == "running"
    assert unrelated is not None and unrelated.work_kind == "candidate"


def test_local_sync_claim_respects_retry_time(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    store.enqueue_work(
        "local_sync",
        local_sync_work.local_sync_work_key(TARGET),
        now=start,
    )
    item = local_sync_work.claim_local_sync_work(store, now=start)
    assert item is not None
    retry = store.fail_work(item, transient=True, now=start)
    assert retry.next_attempt_at == start + timedelta(seconds=60)

    try:
        assert (
            local_sync_work.claim_local_sync_work(store, now=start + timedelta(seconds=59)) is None
        )
        reclaimed = local_sync_work.claim_local_sync_work(store, now=start + timedelta(seconds=60))
    finally:
        store.__exit__(None, None, None)

    assert reclaimed is not None
    assert reclaimed.attempts == 2
    assert reclaimed.status == "running"


def test_local_sync_claim_preserves_order_within_kind(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first_target = "owner/first"
    second_target = "owner/second"
    store.enqueue_work(
        "local_sync",
        local_sync_work.local_sync_work_key(first_target),
        now=start,
    )
    store.enqueue_work(
        "local_sync",
        local_sync_work.local_sync_work_key(second_target),
        now=start + timedelta(seconds=1),
    )

    try:
        first = local_sync_work.claim_local_sync_work(store, now=start + timedelta(seconds=1))
        second = local_sync_work.claim_local_sync_work(store, now=start + timedelta(seconds=1))
    finally:
        store.__exit__(None, None, None)

    assert first is not None
    assert first.work_key == local_sync_work.local_sync_work_key(first_target)
    assert second is not None
    assert second.work_key == local_sync_work.local_sync_work_key(second_target)


def test_local_sync_claim_returns_none_when_only_other_kinds_are_ready(
    tmp_path: Path,
) -> None:
    store = _open_store(tmp_path)
    store.enqueue_work("runtime", "inventory")
    try:
        assert local_sync_work.claim_local_sync_work(store) is None
        unrelated = store.claim_work()
    finally:
        store.__exit__(None, None, None)

    assert unrelated is not None and unrelated.work_kind == "runtime"


def test_local_sync_claim_fails_closed_for_unopened_state(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)

    with pytest.raises(local_sync_work.LocalSyncWorkError, match="claim is invalid"):
        local_sync_work.claim_local_sync_work(store)


@pytest.mark.parametrize(
    "disposition",
    [
        LocalSyncDisposition.INITIALIZED,
        LocalSyncDisposition.PUBLISHED,
        LocalSyncDisposition.NO_CHANGE,
    ],
)
def test_successful_sync_dispositions_complete_claimed_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: LocalSyncDisposition,
) -> None:
    store = _open_store(tmp_path)
    item = _claimed_local_sync(store)
    monkeypatch.setattr(
        local_sync_work,
        "synchronize_local_configuration",
        lambda *args, **kwargs: _sync_result(disposition),
    )

    try:
        result = local_sync_work.execute_claimed_local_sync_work(
            store,
            item,
            tmp_path / "source",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "token",
        )
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "succeeded"
    assert result.synchronization is not None
    assert result.synchronization.disposition is disposition


@pytest.mark.parametrize(
    "disposition",
    [
        LocalSyncDisposition.BASELINE_REQUIRED,
        LocalSyncDisposition.DIVERGED,
        LocalSyncDisposition.REMOTE_MISSING,
    ],
)
def test_deterministic_sync_refusals_block_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: LocalSyncDisposition,
) -> None:
    store = _open_store(tmp_path)
    item = _claimed_local_sync(store)
    monkeypatch.setattr(
        local_sync_work,
        "synchronize_local_configuration",
        lambda *args, **kwargs: _sync_result(disposition),
    )

    try:
        result = local_sync_work.execute_claimed_local_sync_work(
            store,
            item,
            tmp_path / "source",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "token",
        )
        assert local_sync_work.claim_local_sync_work(store) is None
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "blocked"
    assert result.work.next_attempt_at is None


def test_exceptional_sync_failure_uses_bounded_retry_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _open_store(tmp_path)
    item = _claimed_local_sync(store)

    def fail(*args: object, **kwargs: object) -> LocalSyncResult:
        raise LocalSyncError("sanitized")

    monkeypatch.setattr(local_sync_work, "synchronize_local_configuration", fail)
    try:
        result = local_sync_work.execute_claimed_local_sync_work(
            store,
            item,
            tmp_path / "source",
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "token",
        )
    finally:
        store.__exit__(None, None, None)

    assert result.synchronization is None
    assert result.work.status == "retry"
    assert result.work.attempts == 1
    assert result.work.next_attempt_at is not None


def test_executor_rejects_unrelated_claim_without_mutating_it(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.enqueue_work("candidate", "candidate-sha")
    item = store.claim_work()
    assert item is not None and item.work_kind == "candidate"

    try:
        with pytest.raises(local_sync_work.LocalSyncWorkError, match="not an eligible"):
            local_sync_work.execute_claimed_local_sync_work(
                store,
                item,
                tmp_path / "source",
                tmp_path / "snapshots",
                tmp_path / "workspaces",
                TARGET,
                "token",
            )
    finally:
        store.__exit__(None, None, None)

    reopened = _open_store(tmp_path)
    try:
        recovered = reopened.recover_interrupted_work()
        claimed = reopened.claim_work()
    finally:
        reopened.__exit__(None, None, None)
    assert recovered == 1
    assert claimed is not None and claimed.work_kind == "candidate"


def test_interrupted_local_sync_work_remains_recoverable(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    item = _claimed_local_sync(store)
    assert item.status == "running"
    store.__exit__(None, None, None)

    reopened = _open_store(tmp_path)
    try:
        assert reopened.recover_interrupted_work() == 1
        reclaimed = local_sync_work.claim_local_sync_work(reopened)
    finally:
        reopened.__exit__(None, None, None)

    assert reclaimed is not None
    assert reclaimed.work_kind == "local_sync"
    assert reclaimed.status == "running"
    assert reclaimed.attempts == 2
