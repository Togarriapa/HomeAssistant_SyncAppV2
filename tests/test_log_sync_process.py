from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp import log_sync_process
from ha_syncapp.log_artifact import LogRecord, build_log_artifact
from ha_syncapp.log_sync import LogSyncDisposition, LogSyncResult
from ha_syncapp.log_sync_work import LogSyncWorkResult, enqueue_log_sync_work
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REFERENCE = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    store = StateStore(data)
    store.__enter__()
    return store


def _artifact(tmp_path: Path, record_id: str):
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700, exist_ok=True)
    return root, build_log_artifact(
        root,
        (
            LogRecord(
                category="syncapp",
                record_id=record_id,
                timestamp=REFERENCE,
                message=record_id,
            ),
        ),
        reference_time=REFERENCE,
    )


def test_idle_process_does_not_claim_unrelated_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    store.enqueue_work("candidate", "candidate-sha")
    try:
        result = log_sync_process.run_log_sync_process(
            store,
            root,
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "token",
        )
        unrelated = store.claim_work()
    finally:
        store.__exit__(None, None, None)

    assert result.processed is None
    assert unrelated is not None and unrelated.work_kind == "candidate"


def test_valid_claim_executes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    root, artifact = _artifact(tmp_path, "first")
    enqueue_log_sync_work(store, root, TARGET, artifact.artifact_id)
    calls = 0

    def execute(state, item, *args: object) -> LogSyncWorkResult:
        nonlocal calls
        calls += 1
        completed = state.complete_work(item)
        sync = LogSyncResult(
            disposition=LogSyncDisposition.NO_CHANGE,
            target=TARGET,
            repository_id=123,
            branch="logs",
            artifact_id=artifact.artifact_id,
            snapshot_id="a" * 64,
            commit_sha="1" * 40,
            baseline=None,
        )
        return LogSyncWorkResult(completed, sync)

    monkeypatch.setattr(log_sync_process, "execute_claimed_log_sync_work", execute)
    try:
        result = log_sync_process.run_log_sync_process(
            store,
            root,
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "token",
        )
    finally:
        store.__exit__(None, None, None)

    assert calls == 1
    assert result.processed is not None
    assert result.processed.work.status == "succeeded"


def test_foreign_or_malformed_work_key_is_blocked_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    store.enqueue_work("logs", "malformed")
    called = False

    def execute(*args: object, **kwargs: object) -> LogSyncWorkResult:
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(log_sync_process, "execute_claimed_log_sync_work", execute)
    try:
        result = log_sync_process.run_log_sync_process(
            store,
            root,
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "token",
        )
    finally:
        store.__exit__(None, None, None)

    assert called is False
    assert result.processed is not None
    assert result.processed.work.status == "blocked"


def test_normal_process_does_not_recover_interrupted_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root, artifact = _artifact(tmp_path, "first")
    enqueue_log_sync_work(store, root, TARGET, artifact.artifact_id)
    claimed = store.claim_work_kind("logs")
    assert claimed is not None and claimed.status == "running"
    try:
        result = log_sync_process.run_log_sync_process(
            store,
            root,
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "token",
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed is None


def test_execution_error_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    root, artifact = _artifact(tmp_path, "first")
    enqueue_log_sync_work(store, root, TARGET, artifact.artifact_id)
    secret = "github-secret-sentinel"

    def fail(*args: object, **kwargs: object) -> LogSyncWorkResult:
        from ha_syncapp.log_sync_work import LogSyncWorkError

        raise LogSyncWorkError(secret)

    monkeypatch.setattr(log_sync_process, "execute_claimed_log_sync_work", fail)
    try:
        with pytest.raises(log_sync_process.LogSyncProcessError) as caught:
            log_sync_process.run_log_sync_process(
                store,
                root,
                tmp_path / "snapshots",
                tmp_path / "workspaces",
                TARGET,
                secret,
            )
    finally:
        store.__exit__(None, None, None)

    assert secret not in str(caught.value)
