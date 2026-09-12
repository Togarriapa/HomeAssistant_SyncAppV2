from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ha_syncapp.database_history_evidence import DatabaseHistoryRecord
from ha_syncapp.database_history_reader import (
    DatabaseHistoryReadError,
    _fetch_history_records,
    fetch_trusted_database_history_evidence,
)
from ha_syncapp.database_retention import MAX_DATABASE_SNAPSHOTS
from ha_syncapp.github_repo import BranchHead

REFERENCE = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _sha(value: int) -> str:
    return f"{value:040x}"


def _head() -> BranchHead:
    return BranchHead(
        target="owner/private-repo",
        repository_id=123,
        branch="database",
        commit_sha=_sha(1),
    )


def test_reader_rejects_oversized_history_before_accumulating(monkeypatch: pytest.MonkeyPatch) -> None:
    oversized = tuple(
        DatabaseHistoryRecord(sha=_sha(index), committed_at=REFERENCE, parent_shas=())
        for index in range(MAX_DATABASE_SNAPSHOTS + 1)
    )
    monkeypatch.setattr("ha_syncapp.database_history_reader._read_history_page", lambda request: [])
    monkeypatch.setattr("ha_syncapp.database_history_reader._parse_history_page", lambda page: oversized)

    with pytest.raises(DatabaseHistoryReadError, match="evidence limit"):
        _fetch_history_records("owner/private-repo", "secret-token", head_sha=_sha(1))


def test_reader_preserves_sanitized_history_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_syncapp.database_history_reader.fetch_trusted_branch_head",
        lambda *args, **kwargs: _head(),
    )

    def fail(*args: object, **kwargs: object) -> tuple[DatabaseHistoryRecord, ...]:
        raise DatabaseHistoryReadError("GitHub database history transport failed")

    monkeypatch.setattr("ha_syncapp.database_history_reader._fetch_history_records", fail)

    with pytest.raises(DatabaseHistoryReadError, match="history transport failed") as error:
        fetch_trusted_database_history_evidence(
            target="owner/private-repo",
            token="secret-token",
            expected_id=123,
            reference_time=REFERENCE,
            retention_days=7,
        )

    assert "secret-token" not in str(error.value)
