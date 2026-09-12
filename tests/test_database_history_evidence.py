from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.database_history_evidence import (
    DatabaseHistoryEvidenceError,
    DatabaseHistoryRecord,
    validate_trusted_database_history_evidence,
)
from ha_syncapp.database_retention import MAX_DATABASE_SNAPSHOTS
from ha_syncapp.github_repo import BranchHead

REFERENCE = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
HEAD = "a" * 40
PARENT = "b" * 40
ROOT = "c" * 40


def _head(*, branch: str = "database", sha: str = HEAD) -> BranchHead:
    return BranchHead(
        target="owner/home-assistant",
        repository_id=12345,
        branch=branch,
        commit_sha=sha,
    )


def _record(sha: str, age: timedelta, *parents: str) -> DatabaseHistoryRecord:
    return DatabaseHistoryRecord(
        sha=sha,
        committed_at=REFERENCE - age,
        parent_shas=tuple(parents),
    )


def test_valid_history_binds_plan_to_verified_database_head() -> None:
    evidence = validate_trusted_database_history_evidence(
        branch_head=_head(),
        records=(
            _record(HEAD, timedelta(days=1), PARENT),
            _record(PARENT, timedelta(days=7), ROOT),
            _record(ROOT, timedelta(days=8)),
        ),
        reference_time=REFERENCE,
        retention_days=7,
    )

    assert evidence.target == "owner/home-assistant"
    assert evidence.repository_id == 12345
    assert evidence.branch == "database"
    assert evidence.expected_head_sha == HEAD
    assert tuple(record.sha for record in evidence.records) == (HEAD, PARENT, ROOT)
    assert evidence.plan.retained_identities == (HEAD, PARENT)
    assert evidence.plan.prunable_identities == (ROOT,)


def test_history_must_start_at_verified_head() -> None:
    with pytest.raises(DatabaseHistoryEvidenceError, match="verified head"):
        validate_trusted_database_history_evidence(
            branch_head=_head(),
            records=(_record(PARENT, timedelta(days=1)),),
            reference_time=REFERENCE,
            retention_days=7,
        )


@pytest.mark.parametrize("branch", ["main", "candidate", "runtime", "logs", "Database", ""])
def test_history_rejects_every_non_database_branch(branch: str) -> None:
    with pytest.raises(DatabaseHistoryEvidenceError, match="database branch"):
        validate_trusted_database_history_evidence(
            branch_head=_head(branch=branch),
            records=(_record(HEAD, timedelta(days=1)),),
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_history_rejects_merge_commits() -> None:
    with pytest.raises(DatabaseHistoryEvidenceError, match="linear"):
        validate_trusted_database_history_evidence(
            branch_head=_head(),
            records=(
                _record(HEAD, timedelta(days=1), PARENT, ROOT),
                _record(PARENT, timedelta(days=2)),
            ),
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_history_rejects_broken_parent_chain() -> None:
    unrelated = "d" * 40
    with pytest.raises(DatabaseHistoryEvidenceError, match="parent chain"):
        validate_trusted_database_history_evidence(
            branch_head=_head(),
            records=(
                _record(HEAD, timedelta(days=1), unrelated),
                _record(PARENT, timedelta(days=2)),
            ),
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_history_rejects_truncated_evidence() -> None:
    with pytest.raises(DatabaseHistoryEvidenceError, match="complete to the root"):
        validate_trusted_database_history_evidence(
            branch_head=_head(),
            records=(
                _record(HEAD, timedelta(days=1), PARENT),
                _record(PARENT, timedelta(days=2), ROOT),
            ),
            reference_time=REFERENCE,
            retention_days=7,
        )


@pytest.mark.parametrize(
    ("target", "repository_id", "sha"),
    [
        ("", 12345, HEAD),
        ("owner/home-assistant", 0, HEAD),
        ("owner/home-assistant", -1, HEAD),
        ("owner/home-assistant", 12345, "not-a-sha"),
    ],
)
def test_history_rejects_invalid_trusted_repository_identity(
    target: str, repository_id: int, sha: str
) -> None:
    with pytest.raises(DatabaseHistoryEvidenceError, match="trusted"):
        validate_trusted_database_history_evidence(
            branch_head=BranchHead(target, repository_id, "database", sha),
            records=(_record(HEAD, timedelta(days=1)),),
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_history_reuses_retention_timestamp_validation() -> None:
    with pytest.raises(DatabaseHistoryEvidenceError, match="future"):
        validate_trusted_database_history_evidence(
            branch_head=_head(),
            records=(
                DatabaseHistoryRecord(
                    sha=HEAD,
                    committed_at=REFERENCE + timedelta(seconds=1),
                    parent_shas=(),
                ),
            ),
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_history_reuses_configured_retention_bounds() -> None:
    with pytest.raises(DatabaseHistoryEvidenceError, match="retention"):
        validate_trusted_database_history_evidence(
            branch_head=_head(),
            records=(_record(HEAD, timedelta(days=1)),),
            reference_time=REFERENCE,
            retention_days=0,
        )


def test_history_rejects_invalid_record_type() -> None:
    with pytest.raises(DatabaseHistoryEvidenceError, match="invalid evidence"):
        validate_trusted_database_history_evidence(
            branch_head=_head(),
            records=("invalid",),  # type: ignore[arg-type]
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_history_bounds_complete_evidence() -> None:
    records = tuple(
        DatabaseHistoryRecord(
            sha=f"{index:040x}",
            committed_at=REFERENCE - timedelta(seconds=index),
            parent_shas=(f"{index + 1:040x}",),
        )
        for index in range(MAX_DATABASE_SNAPSHOTS + 1)
    )

    with pytest.raises(DatabaseHistoryEvidenceError, match="limit"):
        validate_trusted_database_history_evidence(
            branch_head=_head(sha=records[0].sha),
            records=records,
            reference_time=REFERENCE,
            retention_days=7,
        )
