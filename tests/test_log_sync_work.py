from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp import log_sync_work
from ha_syncapp.log_artifact import LogRecord, build_log_artifact
from ha_syncapp.log_sync import LogSyncDisposition, LogSyncError, LogSyncResult
from ha_syncapp.state import StateStore, WorkItem

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
COMMIT_SHA = "1" * 40
SNAPSHOT_ID = "a" * 64
REFERENCE = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _artifact(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    artifact = build_log_artifact(
        root,
        (
            LogRecord(
                category="syncapp",
                record_id="one",
                timestamp=REFERENCE,
                message="message",
            ),
        ),
        reference_time=REFERENCE,
    )
    return root, artifact


def _result(artifact_id: str, disposition: LogSyncDisposition) -> LogSyncResult:
    return LogSyncResult(
        disposition=disposition,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="logs",
        artifact_id=artifact_id,
        snapshot_id=SNAPSHOT_ID,
        commit_sha=COMMIT_SHA,
        baseline=None,
    )


def _execute(
    store: StateStore,
    item: WorkItem,
    root: Path,
    tmp_path: Path,
) -> log_sync_work.LogSyncWorkResult:
    return log_sync_work.execute_claimed_log_sync_work(
        store,
        item,
        root,
        tmp_path / "snapshots",
        tmp_path / "workspaces",
        TARGET,
        "secret-token",
    )


def test_work_key_round_trips_exact_artifact_and_binds_target() -> None:
    artifact_id = "a" * 64
    key = log_sync_work.log_sync_work_key(TARGET, artifact_id)

    assert len(key) <= 256
    assert log_sync_work.log_sync_artifact_id(TARGET, key) == artifact_id
    with pytest.raises(log_sync_work.LogSyncWorkError, match="does not match"):
        log_sync_work.log_sync_artifact_id("Other/Repo", key)


def test_enqueue_is_idempotent_and_requires_verified_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root, artifact = _artifact(tmp_path)
    try:
        first = log_sync_work.enqueue_log_sync_work(store, root, TARGET, artifact.artifact_id)
        second = log_sync_work.enqueue_log_sync_work(store, root, TARGET, artifact.artifact_id)
        with pytest.raises(log_sync_work.LogSyncWorkError):
            log_sync_work.enqueue_log_sync_work(store, root, TARGET, "0" * 64)
    finally:
        store.__exit__(None, None, None)

    assert first.work_kind == "logs"
    assert second.work_key == first.work_key
    assert second.status == "pending"


@pytest.mark.parametrize(
    "disposition",
    [
        LogSyncDisposition.INITIALIZED,
        LogSyncDisposition.PUBLISHED,
        LogSyncDisposition.NO_CHANGE,
    ],
)
def test_successful_log_outcomes_complete_exact_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: LogSyncDisposition,
) -> None:
    store = _store(tmp_path)
    root, artifact = _artifact(tmp_path)
    log_sync_work.enqueue_log_sync_work(store, root, TARGET, artifact.artifact_id)
    item = log_sync_work.claim_log_sync_work(store)
    assert item is not None

    def synchronize(*args: object, **kwargs: object) -> LogSyncResult:
        return _result(artifact.artifact_id, disposition)

    monkeypatch.setattr(log_sync_work, "synchronize_log_artifact", synchronize)
    try:
        result = _execute(store, item, root, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "succeeded"
    assert result.synchronization is not None


@pytest.mark.parametrize(
    "disposition",
    [
        LogSyncDisposition.BASELINE_REQUIRED,
        LogSyncDisposition.DIVERGED,
        LogSyncDisposition.REMOTE_MISSING,
    ],
)
def test_deterministic_publication_refusals_block_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: LogSyncDisposition,
) -> None:
    store = _store(tmp_path)
    root, artifact = _artifact(tmp_path)
    log_sync_work.enqueue_log_sync_work(store, root, TARGET, artifact.artifact_id)
    item = log_sync_work.claim_log_sync_work(store)
    assert item is not None

    monkeypatch.setattr(
        log_sync_work,
        "synchronize_log_artifact",
        lambda *args, **kwargs: _result(artifact.artifact_id, disposition),
    )
    try:
        result = _execute(store, item, root, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "blocked"
    assert result.work.next_attempt_at is None


def test_missing_or_tampered_exact_artifact_blocks_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    root, artifact = _artifact(tmp_path)
    log_sync_work.enqueue_log_sync_work(store, root, TARGET, artifact.artifact_id)
    item = log_sync_work.claim_log_sync_work(store)
    assert item is not None
    (artifact.root / "manifest.json").unlink()
    called = False

    def synchronize(*args: object, **kwargs: object) -> LogSyncResult:
        nonlocal called
        called = True
        return _result(artifact.artifact_id, LogSyncDisposition.PUBLISHED)

    monkeypatch.setattr(log_sync_work, "synchronize_log_artifact", synchronize)
    try:
        result = _execute(store, item, root, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "blocked"
    assert result.work.next_attempt_at is None
    assert result.synchronization is None
    assert not called


def test_transport_failure_uses_existing_transient_retry_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    root, artifact = _artifact(tmp_path)
    log_sync_work.enqueue_log_sync_work(store, root, TARGET, artifact.artifact_id)
    item = log_sync_work.claim_log_sync_work(store)
    assert item is not None
    secret = "ghp_super_secret_value"

    def fail(*args: object, **kwargs: object) -> LogSyncResult:
        raise LogSyncError(secret)

    monkeypatch.setattr(log_sync_work, "synchronize_log_artifact", fail)
    try:
        result = log_sync_work.execute_claimed_log_sync_work(
            store,
            item,
            root,
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
