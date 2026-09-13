from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.database_history_evidence import (
    DatabaseHistoryRecord,
    TrustedDatabaseHistoryEvidence,
    validate_trusted_database_history_evidence,
)
from ha_syncapp.database_history_prewrite import (
    DatabaseHistoryPrewriteError,
    reprove_database_history_prewrite,
)
from ha_syncapp.github_repo import BranchHead, RepositoryVerificationError

REFERENCE = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)


def _sha(value: int) -> str:
    return f"{value:040x}"


def _evidence() -> TrustedDatabaseHistoryEvidence:
    return validate_trusted_database_history_evidence(
        branch_head=BranchHead(
            target="owner/private-repo",
            repository_id=123,
            branch="database",
            commit_sha=_sha(3),
        ),
        records=(
            DatabaseHistoryRecord(
                _sha(3), REFERENCE - timedelta(days=1), (_sha(2),)
            ),
            DatabaseHistoryRecord(
                _sha(2), REFERENCE - timedelta(days=5), (_sha(1),)
            ),
            DatabaseHistoryRecord(
                _sha(1), REFERENCE - timedelta(days=10), ()
            ),
        ),
        reference_time=REFERENCE,
        retention_days=7,
    )


def test_prewrite_reproves_exact_repository_and_database_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int, str]] = []

    def fake_fetch(
        target: str, token: str, *, expected_id: int, branch: str
    ) -> BranchHead:
        calls.append((target, token, expected_id, branch))
        return BranchHead(
            target="owner/private-repo",
            repository_id=123,
            branch="database",
            commit_sha=_sha(3),
        )

    monkeypatch.setattr(
        "ha_syncapp.database_history_prewrite.fetch_trusted_branch_head", fake_fetch
    )

    proof = reprove_database_history_prewrite(
        evidence=_evidence(), token="secret-token"
    )

    assert calls == [("owner/private-repo", "secret-token", 123, "database")]
    assert proof.target == "owner/private-repo"
    assert proof.repository_id == 123
    assert proof.branch == "database"
    assert proof.expected_head_sha == _sha(3)


def test_prewrite_rejects_moved_remote_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_syncapp.database_history_prewrite.fetch_trusted_branch_head",
        lambda *args, **kwargs: BranchHead(
            target="owner/private-repo",
            repository_id=123,
            branch="database",
            commit_sha=_sha(4),
        ),
    )

    with pytest.raises(DatabaseHistoryPrewriteError, match="changed before replacement"):
        reprove_database_history_prewrite(evidence=_evidence(), token="secret-token")


@pytest.mark.parametrize(
    ("target", "repository_id", "branch"),
    [
        ("other/private-repo", 123, "database"),
        ("owner/private-repo", 999, "database"),
        ("owner/private-repo", 123, "main"),
    ],
)
def test_prewrite_rejects_changed_remote_identity_or_branch(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    repository_id: int,
    branch: str,
) -> None:
    monkeypatch.setattr(
        "ha_syncapp.database_history_prewrite.fetch_trusted_branch_head",
        lambda *args, **kwargs: BranchHead(
            target=target,
            repository_id=repository_id,
            branch=branch,
            commit_sha=_sha(3),
        ),
    )

    with pytest.raises(DatabaseHistoryPrewriteError, match="changed before replacement"):
        reprove_database_history_prewrite(evidence=_evidence(), token="secret-token")


def test_prewrite_sanitizes_repository_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> BranchHead:
        raise RepositoryVerificationError("secret-token should not escape")

    monkeypatch.setattr(
        "ha_syncapp.database_history_prewrite.fetch_trusted_branch_head", fail
    )

    with pytest.raises(DatabaseHistoryPrewriteError) as caught:
        reprove_database_history_prewrite(evidence=_evidence(), token="secret-token")

    assert str(caught.value) == "trusted database repository verification failed"
    assert "secret-token" not in str(caught.value)


def test_prewrite_rejects_forged_non_database_evidence() -> None:
    evidence = replace(_evidence(), branch="logs")

    with pytest.raises(DatabaseHistoryPrewriteError, match="database branch"):
        reprove_database_history_prewrite(evidence=evidence, token="secret-token")


def test_prewrite_rejects_inconsistent_evidence_head() -> None:
    evidence = _evidence()
    forged = replace(evidence, expected_head_sha=_sha(9))

    with pytest.raises(DatabaseHistoryPrewriteError, match="head is inconsistent"):
        reprove_database_history_prewrite(evidence=forged, token="secret-token")
