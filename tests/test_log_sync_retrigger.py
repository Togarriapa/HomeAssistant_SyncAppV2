from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ha_syncapp import log_sync_process, log_sync_retrigger
from ha_syncapp.log_artifact import LogRecord, build_log_artifact
from ha_syncapp.log_sync import LogSyncDisposition, LogSyncResult
from ha_syncapp.log_sync_work import enqueue_log_sync_work
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REFERENCE = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
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


def test_retrigger_recovers_interrupted_and_processes_at_most_one_log_item(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    root, first = _artifact(tmp_path, "first")
    _, second = _artifact(tmp_path, "second")
    enqueue_log_sync_work(store, root, TARGET, first.artifact_id)
    enqueue_log_sync_work(store, root, TARGET, second.artifact_id)
    interrupted = store.claim_work_kind("logs")
    assert interrupted is not None
    processed_ids: list[str] = []

    def execute(store, item, artifact_root, snapshot_root, workspace_root, target, token):
        del artifact_root, snapshot_root, workspace_root, target, token
        from ha_syncapp.log_sync_work import LogSyncWorkResult, log_sync_artifact_id

        artifact_id = log_sync_artifact_id(TARGET, item.work_key)
        processed_ids.append(artifact_id)
        completed = store.complete_work(item)
        sync = LogSyncResult(
            disposition=LogSyncDisposition.NO_CHANGE,
            target=TARGET,
            repository_id=123,
            branch="logs",
            artifact_id=artifact_id,
            snapshot_id="a" * 64,
            commit_sha="1" * 40,
            baseline=None,
        )
        return LogSyncWorkResult(completed, sync)

    monkeypatch.setattr(log_sync_process, "execute_claimed_log_sync_work", execute)
    try:
        result = log_sync_retrigger.run_log_sync_retrigger_pass(
            store,
            root,
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "token",
        )
        remaining = store.claim_work_kind("logs")
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 1
    assert len(processed_ids) == 1
    assert remaining is not None


def test_retrigger_blocks_malformed_logs_work_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    store.enqueue_work("logs", "malformed")
    try:
        result = log_sync_retrigger.run_log_sync_retrigger_pass(
            store,
            root,
            tmp_path / "snapshots",
            tmp_path / "workspaces",
            TARGET,
            "token",
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == "blocked"
    assert result.processed.work.next_attempt_at is None
