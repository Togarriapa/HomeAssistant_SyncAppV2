from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.log_history_evidence import LogHistoryRecord, validate_trusted_log_history_evidence
from ha_syncapp.log_retention_work import (
    LOG_RETENTION_WORK_KIND,
    LogRetentionWorkDisposition,
    log_retention_work_key,
    run_log_retention_work_pass,
)
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
HEAD = "a" * 40
MID = "b" * 40
ROOT = "c" * 40
REPLACEMENT = "d" * 40
SNAPSHOT = "f" * 64
REFERENCE = datetime(2026, 9, 13, tzinfo=UTC)


def _evidence():
    return validate_trusted_log_history_evidence(
        branch_head=BranchHead(TARGET, 123, "logs", HEAD),
        records=(
            LogHistoryRecord(HEAD, REFERENCE - timedelta(days=1), (MID,)),
            LogHistoryRecord(MID, REFERENCE - timedelta(days=10), (ROOT,)),
            LogHistoryRecord(ROOT, REFERENCE - timedelta(days=45), ()),
        ),
        reference_time=REFERENCE,
    )


def _store_with_interrupted_intent(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    store.record_synchronization_baseline(
        TARGET, "logs", SNAPSHOT, HEAD, synchronized_at=REFERENCE
    )
    work_key = log_retention_work_key(_evidence())
    store.enqueue_work(LOG_RETENTION_WORK_KIND, work_key, now=REFERENCE)
    item = store.claim_work_kind(LOG_RETENTION_WORK_KIND, now=REFERENCE)
    assert item is not None
    store.record_log_retention_intent(
        item,
        TARGET,
        123,
        HEAD,
        REPLACEMENT,
        SNAPSHOT,
        recorded_at=REFERENCE,
    )
    return store


def test_post_publication_crash_reconciles_without_duplicate_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store_with_interrupted_intent(tmp_path)
    remote = Mock(return_value=BranchHead(TARGET, 123, "logs", REPLACEMENT))
    publish = Mock(side_effect=AssertionError("reconciliation must not publish again"))
    monkeypatch.setattr("ha_syncapp.log_retention_work.fetch_trusted_branch_head", remote)
    monkeypatch.setattr("ha_syncapp.log_retention_work.replace_logs_history", publish)
    try:
        result = run_log_retention_work_pass(
            store,
            tmp_path / "staging",
            TARGET,
            "test-token",
            reference_time=REFERENCE + timedelta(minutes=1),
            recover_interrupted=True,
        )

        assert result.recovered_interrupted == 1
        assert result.processed is not None
        assert result.processed.work.status == "succeeded"
        assert result.processed.disposition is LogRetentionWorkDisposition.REPLACED
        baseline = store.synchronization_baseline(TARGET, "logs")
        assert baseline is not None
        assert baseline.snapshot_id == SNAPSHOT
        assert baseline.commit_sha == REPLACEMENT
        publish.assert_not_called()
        remote.assert_called_once()
    finally:
        store.__exit__(None, None, None)


def test_divergent_remote_after_intent_blocks_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store_with_interrupted_intent(tmp_path)
    divergent = "e" * 40
    monkeypatch.setattr(
        "ha_syncapp.log_retention_work.fetch_trusted_branch_head",
        Mock(return_value=BranchHead(TARGET, 123, "logs", divergent)),
    )
    publish = Mock(side_effect=AssertionError("divergent remote must not be rewritten"))
    monkeypatch.setattr("ha_syncapp.log_retention_work.replace_logs_history", publish)
    try:
        result = run_log_retention_work_pass(
            store,
            tmp_path / "staging",
            TARGET,
            "test-token",
            reference_time=REFERENCE + timedelta(minutes=1),
            recover_interrupted=True,
        )

        assert result.processed is not None
        assert result.processed.work.status == "blocked"
        assert result.processed.disposition is LogRetentionWorkDisposition.STALE
        baseline = store.synchronization_baseline(TARGET, "logs")
        assert baseline is not None
        assert baseline.commit_sha == HEAD
        publish.assert_not_called()
    finally:
        store.__exit__(None, None, None)
