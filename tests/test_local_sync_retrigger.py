from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp import local_sync_retrigger, local_sync_work
from ha_syncapp.local_sync import LocalSyncDisposition, LocalSyncResult
from ha_syncapp.local_sync_work import LocalSyncWorkResult
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


def _run(
    store: StateStore,
    tmp_path: Path,
) -> local_sync_retrigger.LocalSyncRetriggerResult:
    return local_sync_retrigger.run_local_sync_retrigger_pass(
        store,
        tmp_path / "source",
        tmp_path / "snapshots",
        tmp_path / "workspaces",
        TARGET,
        "token",
    )


def test_retrigger_pass_returns_no_work_without_consuming_other_kinds(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    store.enqueue_work("candidate", "candidate-sha")
    try:
        result = _run(store, tmp_path)
        unrelated = store.claim_work()
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 0
    assert result.processed is None
    assert unrelated is not None and unrelated.work_kind == "candidate"


def test_retrigger_pass_recovers_interrupted_local_sync_before_claiming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _open_store(tmp_path)
    local_sync_work.enqueue_local_sync_work(store, TARGET)
    claimed = local_sync_work.claim_local_sync_work(store)
    assert claimed is not None and claimed.status == "running"

    def succeed(
        state: StateStore,
        item,
        *args: object,
        **kwargs: object,
    ) -> LocalSyncWorkResult:
        assert item.status == "running"
        assert item.attempts == 2
        completed = state.complete_work(item)
        return LocalSyncWorkResult(completed, _sync_result(LocalSyncDisposition.NO_CHANGE))

    monkeypatch.setattr(local_sync_retrigger, "execute_claimed_local_sync_work", succeed)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 1
    assert result.processed is not None
    assert result.processed.work.status == "succeeded"
    assert result.processed.work.attempts == 2


def test_retrigger_pass_processes_at_most_one_local_sync_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _open_store(tmp_path)
    first_target = TARGET
    second_target = "Owner/Second-Private-Home"
    local_sync_work.enqueue_local_sync_work(store, first_target)
    local_sync_work.enqueue_local_sync_work(store, second_target)

    def succeed(
        state: StateStore,
        item,
        *args: object,
        **kwargs: object,
    ) -> LocalSyncWorkResult:
        completed = state.complete_work(item)
        return LocalSyncWorkResult(completed, _sync_result(LocalSyncDisposition.NO_CHANGE))

    monkeypatch.setattr(local_sync_retrigger, "execute_claimed_local_sync_work", succeed)
    try:
        result = _run(store, tmp_path)
        remaining = local_sync_work.claim_local_sync_work(store)
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert remaining is not None
    assert remaining.work_key == local_sync_work.local_sync_work_key(second_target)


def test_retrigger_pass_preserves_deterministic_block_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _open_store(tmp_path)
    local_sync_work.enqueue_local_sync_work(store, TARGET)
    monkeypatch.setattr(
        local_sync_work,
        "synchronize_local_configuration",
        lambda *args, **kwargs: _sync_result(LocalSyncDisposition.DIVERGED),
    )

    try:
        result = _run(store, tmp_path)
        assert local_sync_work.claim_local_sync_work(store) is None
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == "blocked"
    assert result.processed.work.next_attempt_at is None


def test_retrigger_pass_preserves_transient_retry_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _open_store(tmp_path)
    local_sync_work.enqueue_local_sync_work(store, TARGET)

    def fail(*args: object, **kwargs: object) -> LocalSyncResult:
        from ha_syncapp.local_sync import LocalSyncError

        raise LocalSyncError("sanitized")

    monkeypatch.setattr(local_sync_work, "synchronize_local_configuration", fail)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == "retry"
    assert result.processed.work.attempts == 1
    assert result.processed.work.next_attempt_at is not None


def test_retrigger_pass_fails_closed_for_unopened_state(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)

    with pytest.raises(
        local_sync_retrigger.LocalSyncRetriggerError,
        match="failed closed",
    ):
        _run(store, tmp_path)


def test_retrigger_pass_does_not_expose_token_in_failure(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    local_sync_work.enqueue_local_sync_work(store, TARGET)
    secret = "ghp_super_secret_value"

    def fail(*args: object, **kwargs: object) -> LocalSyncWorkResult:
        raise local_sync_work.LocalSyncWorkError(secret)

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(local_sync_retrigger, "execute_claimed_local_sync_work", fail)
            with pytest.raises(local_sync_retrigger.LocalSyncRetriggerError) as error:
                local_sync_retrigger.run_local_sync_retrigger_pass(
                    store,
                    tmp_path / "source",
                    tmp_path / "snapshots",
                    tmp_path / "workspaces",
                    TARGET,
                    secret,
                )
    finally:
        store.__exit__(None, None, None)

    assert secret not in str(error.value)


def test_retrigger_pass_uses_queue_order_for_local_sync(tmp_path: Path) -> None:
    store = _open_store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first_target = TARGET
    second_target = "Owner/Second-Private-Home"
    store.enqueue_work(
        "local_sync",
        local_sync_work.local_sync_work_key(first_target),
        now=start,
    )
    store.enqueue_work(
        "local_sync",
        local_sync_work.local_sync_work_key(second_target),
        now=start,
    )

    try:
        first = local_sync_work.claim_local_sync_work(store, now=start)
    finally:
        store.__exit__(None, None, None)

    assert first is not None
    expected = min(
        local_sync_work.local_sync_work_key(first_target),
        local_sync_work.local_sync_work_key(second_target),
    )
    assert first.work_key == expected
