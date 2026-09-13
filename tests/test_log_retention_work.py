from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.log_history_evidence import LogHistoryRecord, validate_trusted_log_history_evidence
from ha_syncapp.log_retention_work import (
    LOG_RETENTION_WORK_KIND,
    claim_log_retention_work,
    discover_log_retention_work,
    log_retention_work_key,
)
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
TOKEN = "github-secret-sentinel"
HEAD = "a" * 40
MID = "b" * 40
ROOT = "c" * 40
REFERENCE = datetime(2026, 9, 13, tzinfo=UTC)


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    return store


def _evidence(*, reference_time: datetime = REFERENCE):
    return validate_trusted_log_history_evidence(
        branch_head=BranchHead(TARGET, 123, "logs", HEAD),
        records=(
            LogHistoryRecord(HEAD, REFERENCE - timedelta(days=1), (MID,)),
            LogHistoryRecord(MID, REFERENCE - timedelta(days=10), (ROOT,)),
            LogHistoryRecord(ROOT, REFERENCE - timedelta(days=45), ()),
        ),
        reference_time=reference_time,
    )


def test_log_retention_work_identity_is_deterministic_and_head_bound() -> None:
    evidence = _evidence()

    first = log_retention_work_key(evidence)
    second = log_retention_work_key(evidence)

    assert LOG_RETENTION_WORK_KIND == "logs_retention"
    assert first == second
    assert first.startswith(f"{HEAD}:")
    assert len(first) == 105


def test_same_head_and_retention_outcome_converge_across_discovery_times() -> None:
    first = _evidence(reference_time=REFERENCE)
    later = _evidence(reference_time=REFERENCE + timedelta(hours=1))

    assert first.plan.retained_shas == later.plan.retained_shas
    assert first.plan.pruned_shas == later.plan.pruned_shas
    assert log_retention_work_key(first) == log_retention_work_key(later)


def test_log_retention_work_identity_rejects_non_logs_evidence() -> None:
    evidence = _evidence()
    forged = object.__new__(type(evidence))
    for name in ("target", "repository_id", "expected_head_sha", "commits", "plan"):
        object.__setattr__(forged, name, getattr(evidence, name))
    object.__setattr__(forged, "branch", "main")

    with pytest.raises(ValueError, match="identity"):
        log_retention_work_key(forged)


def test_discovery_of_same_exact_logs_plan_converges_on_one_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    evidence = _evidence()
    fetch = Mock(return_value=evidence)
    monkeypatch.setattr("ha_syncapp.log_retention_work.fetch_trusted_log_history_evidence", fetch)
    try:
        first = discover_log_retention_work(
            store,
            TARGET,
            TOKEN,
            reference_time=REFERENCE,
        )
        second = discover_log_retention_work(
            store,
            TARGET,
            TOKEN,
            reference_time=REFERENCE,
        )
        claimed = claim_log_retention_work(store, now=REFERENCE)

        assert first == second
        assert claimed is not None
        assert claimed.work_kind == LOG_RETENTION_WORK_KIND
        assert claim_log_retention_work(store, now=REFERENCE) is None
        assert fetch.call_count == 2
    finally:
        store.__exit__(None, None, None)


def test_discovery_requires_pinned_repo_identity(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    try:
        with pytest.raises(ValueError, match="not pinned"):
            discover_log_retention_work(
                store,
                TARGET,
                TOKEN,
                reference_time=REFERENCE,
            )
    finally:
        store.__exit__(None, None, None)
