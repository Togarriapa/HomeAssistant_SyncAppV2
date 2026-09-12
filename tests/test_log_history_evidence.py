from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.log_history_evidence import (
    LogHistoryEvidenceError,
    LogHistoryRecord,
    validate_trusted_log_history_evidence,
)

REFERENCE = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _sha(value: int) -> str:
    return f"{value:040x}"


def _head(*, branch: str = "logs", sha: str | None = None) -> BranchHead:
    return BranchHead(
        target="owner/private-repo",
        repository_id=123,
        branch=branch,
        commit_sha=sha or _sha(3),
    )


def _record(value: int, age: timedelta, *parents: int) -> LogHistoryRecord:
    return LogHistoryRecord(
        sha=_sha(value),
        committed_at=REFERENCE - age,
        parent_shas=tuple(_sha(parent) for parent in parents),
    )


def test_evidence_binds_linear_complete_history_to_verified_logs_head() -> None:
    evidence = validate_trusted_log_history_evidence(
        branch_head=_head(),
        records=(
            _record(3, timedelta(days=1), 2),
            _record(2, timedelta(days=20), 1),
            _record(1, timedelta(days=60)),
        ),
        reference_time=REFERENCE,
    )

    assert evidence.target == "owner/private-repo"
    assert evidence.repository_id == 123
    assert evidence.branch == "logs"
    assert evidence.expected_head_sha == _sha(3)
    assert tuple(commit.sha for commit in evidence.commits) == (_sha(3), _sha(2), _sha(1))
    assert evidence.plan.expected_head_sha == _sha(3)
    assert evidence.plan.retained_shas == (_sha(3), _sha(2))
    assert evidence.plan.pruned_shas == (_sha(1),)


def test_evidence_rejects_non_logs_branch() -> None:
    with pytest.raises(LogHistoryEvidenceError, match="logs branch"):
        validate_trusted_log_history_evidence(
            branch_head=_head(branch="main"),
            records=(_record(3, timedelta(days=1)),),
            reference_time=REFERENCE,
        )


def test_evidence_rejects_invalid_repository_identity() -> None:
    with pytest.raises(LogHistoryEvidenceError, match="repository identity"):
        validate_trusted_log_history_evidence(
            branch_head=BranchHead(
                target="owner/private-repo",
                repository_id=0,
                branch="logs",
                commit_sha=_sha(3),
            ),
            records=(_record(3, timedelta(days=1)),),
            reference_time=REFERENCE,
        )


def test_evidence_requires_exact_verified_head() -> None:
    with pytest.raises(LogHistoryEvidenceError, match="head"):
        validate_trusted_log_history_evidence(
            branch_head=_head(sha=_sha(9)),
            records=(_record(3, timedelta(days=1)),),
            reference_time=REFERENCE,
        )


def test_evidence_rejects_empty_history() -> None:
    with pytest.raises(LogHistoryEvidenceError, match="history"):
        validate_trusted_log_history_evidence(
            branch_head=_head(),
            records=(),
            reference_time=REFERENCE,
        )


def test_evidence_rejects_merge_history() -> None:
    with pytest.raises(LogHistoryEvidenceError, match="linear"):
        validate_trusted_log_history_evidence(
            branch_head=_head(),
            records=(
                _record(3, timedelta(days=1), 2, 8),
                _record(2, timedelta(days=2)),
            ),
            reference_time=REFERENCE,
        )


def test_evidence_rejects_broken_parent_chain() -> None:
    with pytest.raises(LogHistoryEvidenceError, match="parent chain"):
        validate_trusted_log_history_evidence(
            branch_head=_head(),
            records=(
                _record(3, timedelta(days=1), 9),
                _record(2, timedelta(days=2)),
            ),
            reference_time=REFERENCE,
        )


def test_evidence_rejects_truncated_history() -> None:
    with pytest.raises(LogHistoryEvidenceError, match="complete"):
        validate_trusted_log_history_evidence(
            branch_head=_head(),
            records=(
                _record(3, timedelta(days=1), 2),
                _record(2, timedelta(days=2), 1),
            ),
            reference_time=REFERENCE,
        )


def test_evidence_reuses_planner_validation_for_duplicate_sha() -> None:
    with pytest.raises(LogHistoryEvidenceError, match="duplicate"):
        validate_trusted_log_history_evidence(
            branch_head=_head(),
            records=(
                _record(3, timedelta(days=1), 2),
                LogHistoryRecord(
                    sha=_sha(2),
                    committed_at=REFERENCE - timedelta(days=2),
                    parent_shas=(_sha(2),),
                ),
                _record(2, timedelta(days=3)),
            ),
            reference_time=REFERENCE,
        )


def test_evidence_reuses_planner_validation_for_future_timestamp() -> None:
    with pytest.raises(LogHistoryEvidenceError, match="future"):
        validate_trusted_log_history_evidence(
            branch_head=_head(),
            records=(
                LogHistoryRecord(
                    sha=_sha(3),
                    committed_at=REFERENCE + timedelta(seconds=1),
                    parent_shas=(),
                ),
            ),
            reference_time=REFERENCE,
        )
