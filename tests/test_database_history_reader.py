from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.database_history_evidence import DatabaseHistoryRecord
from ha_syncapp.database_history_reader import (
    DatabaseHistoryReadError,
    fetch_trusted_database_history_evidence,
)
from ha_syncapp.github_repo import BranchHead, RepositoryVerificationError

REFERENCE = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _sha(value: int) -> str:
    return f"{value:040x}"


def _head(*, sha: str | None = None) -> BranchHead:
    return BranchHead(
        target="owner/private-repo",
        repository_id=123,
        branch="database",
        commit_sha=sha or _sha(3),
    )


def _record(value: int, age: timedelta, *parents: int) -> DatabaseHistoryRecord:
    return DatabaseHistoryRecord(
        sha=_sha(value),
        committed_at=REFERENCE - age,
        parent_shas=tuple(_sha(parent) for parent in parents),
    )


def test_reader_reproves_repo_and_binds_complete_history(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_head(target: str, token: str, *, expected_id: int, branch: str) -> BranchHead:
        calls.append(("head", target, token, expected_id, branch))
        return _head()

    def fake_records(
        target: str, token: str, *, head_sha: str
    ) -> tuple[DatabaseHistoryRecord, ...]:
        calls.append(("records", target, token, head_sha))
        return (
            _record(3, timedelta(days=1), 2),
            _record(2, timedelta(days=7), 1),
            _record(1, timedelta(days=8)),
        )

    monkeypatch.setattr("ha_syncapp.database_history_reader.fetch_trusted_branch_head", fake_head)
    monkeypatch.setattr("ha_syncapp.database_history_reader._fetch_history_records", fake_records)

    evidence = fetch_trusted_database_history_evidence(
        target="owner/private-repo",
        token="secret-token",
        expected_id=123,
        reference_time=REFERENCE,
        retention_days=7,
    )

    assert calls == [
        ("head", "owner/private-repo", "secret-token", 123, "database"),
        ("records", "owner/private-repo", "secret-token", _sha(3)),
    ]
    assert evidence.expected_head_sha == _sha(3)
    assert evidence.plan.retained_identities == (_sha(3), _sha(2))
    assert evidence.plan.prunable_identities == (_sha(1),)


def test_reader_uses_exact_verified_sha_not_moving_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []
    monkeypatch.setattr(
        "ha_syncapp.database_history_reader.fetch_trusted_branch_head",
        lambda *args, **kwargs: _head(sha=_sha(9)),
    )

    def fake_records(
        target: str, token: str, *, head_sha: str
    ) -> tuple[DatabaseHistoryRecord, ...]:
        observed.append(head_sha)
        return (DatabaseHistoryRecord(sha=_sha(9), committed_at=REFERENCE, parent_shas=()),)

    monkeypatch.setattr("ha_syncapp.database_history_reader._fetch_history_records", fake_records)

    fetch_trusted_database_history_evidence(
        target="owner/private-repo",
        token="secret-token",
        expected_id=123,
        reference_time=REFERENCE,
        retention_days=7,
    )

    assert observed == [_sha(9)]


def test_reader_sanitizes_repository_verification_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> BranchHead:
        raise RepositoryVerificationError("transport leaked secret-token")

    monkeypatch.setattr("ha_syncapp.database_history_reader.fetch_trusted_branch_head", fail)

    with pytest.raises(DatabaseHistoryReadError, match="repository verification failed") as error:
        fetch_trusted_database_history_evidence(
            target="owner/private-repo",
            token="secret-token",
            expected_id=123,
            reference_time=REFERENCE,
            retention_days=7,
        )

    assert "secret-token" not in str(error.value)


def test_parse_history_page_accepts_minimal_commit_metadata() -> None:
    from ha_syncapp.database_history_reader import _parse_history_page

    records = _parse_history_page(
        [
            {
                "sha": _sha(3),
                "commit": {"committer": {"date": "2026-09-11T12:00:00Z"}},
                "parents": [{"sha": _sha(2)}],
            },
            {
                "sha": _sha(2),
                "commit": {"committer": {"date": "2026-09-05T10:30:00+00:00"}},
                "parents": [],
            },
        ]
    )

    assert records == (
        DatabaseHistoryRecord(
            sha=_sha(3),
            committed_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
            parent_shas=(_sha(2),),
        ),
        DatabaseHistoryRecord(
            sha=_sha(2),
            committed_at=datetime(2026, 9, 5, 10, 30, tzinfo=UTC),
            parent_shas=(),
        ),
    )


@pytest.mark.parametrize(
    "page",
    [
        {},
        [{"sha": _sha(1), "commit": {}, "parents": []}],
        [{"sha": _sha(1), "commit": {"committer": {"date": 7}}, "parents": []}],
        [{"sha": _sha(1), "commit": {"committer": {"date": "not-a-date"}}, "parents": []}],
        [{"sha": _sha(1), "commit": {"committer": {"date": "2026-09-11T12:00:00"}}, "parents": []}],
        [
            {
                "sha": _sha(1),
                "commit": {"committer": {"date": "2026-09-11T12:00:00Z"}},
                "parents": [{}],
            }
        ],
    ],
)
def test_parse_history_page_rejects_malformed_metadata(page: object) -> None:
    from ha_syncapp.database_history_reader import _parse_history_page

    with pytest.raises(DatabaseHistoryReadError, match="invalid database history metadata"):
        _parse_history_page(page)
