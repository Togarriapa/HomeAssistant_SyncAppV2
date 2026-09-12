from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.github_repo import BranchHead, RepositoryVerificationError
from ha_syncapp.log_history_evidence import (
    LogHistoryRecord,
    TrustedLogHistoryEvidence,
    validate_trusted_log_history_evidence,
)
from ha_syncapp.log_history_prewrite import (
    LogHistoryPrewriteError,
    reprove_log_history_prewrite,
)

REFERENCE = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)


def _sha(value: int) -> str:
    return f"{value:040x}"


def _evidence() -> TrustedLogHistoryEvidence:
    return validate_trusted_log_history_evidence(
        branch_head=BranchHead(
            target="owner/private-repo",
            repository_id=123,
            branch="logs",
            commit_sha=_sha(3),
        ),
        records=(
            LogHistoryRecord(
                sha=_sha(3),
                committed_at=REFERENCE - timedelta(days=1),
                parent_shas=(_sha(2),),
            ),
            LogHistoryRecord(
                sha=_sha(2),
                committed_at=REFERENCE - timedelta(days=10),
                parent_shas=(_sha(1),),
            ),
            LogHistoryRecord(
                sha=_sha(1),
                committed_at=REFERENCE - timedelta(days=40),
                parent_shas=(),
            ),
        ),
        reference_time=REFERENCE,
    )


def test_prewrite_reproves_exact_repository_and_logs_head(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, int, str]] = []

    def fake_fetch(target: str, token: str, *, expected_id: int, branch: str) -> BranchHead:
        calls.append((target, token, expected_id, branch))
        return BranchHead(
            target="owner/private-repo",
            repository_id=123,
            branch="logs",
            commit_sha=_sha(3),
        )

    monkeypatch.setattr("ha_syncapp.log_history_prewrite.fetch_trusted_branch_head", fake_fetch)

    proof = reprove_log_history_prewrite(evidence=_evidence(), token="secret-token")

    assert calls == [("owner/private-repo", "secret-token", 123, "logs")]
    assert proof.target == "owner/private-repo"
    assert proof.repository_id == 123
    assert proof.branch == "logs"
    assert proof.expected_head_sha == _sha(3)


def test_prewrite_rejects_moved_remote_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_syncapp.log_history_prewrite.fetch_trusted_branch_head",
        lambda *args, **kwargs: BranchHead(
            target="owner/private-repo",
            repository_id=123,
            branch="logs",
            commit_sha=_sha(4),
        ),
    )

    with pytest.raises(LogHistoryPrewriteError, match="changed before replacement"):
        reprove_log_history_prewrite(evidence=_evidence(), token="secret-token")


@pytest.mark.parametrize(
    ("target", "repository_id", "branch"),
    [
        ("other/private-repo", 123, "logs"),
        ("owner/private-repo", 999, "logs"),
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
        "ha_syncapp.log_history_prewrite.fetch_trusted_branch_head",
        lambda *args, **kwargs: BranchHead(
            target=target,
            repository_id=repository_id,
            branch=branch,
            commit_sha=_sha(3),
        ),
    )

    with pytest.raises(LogHistoryPrewriteError, match="changed before replacement"):
        reprove_log_history_prewrite(evidence=_evidence(), token="secret-token")


def test_prewrite_sanitizes_repository_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> BranchHead:
        raise RepositoryVerificationError("secret-token should not escape")

    monkeypatch.setattr("ha_syncapp.log_history_prewrite.fetch_trusted_branch_head", fail)

    with pytest.raises(LogHistoryPrewriteError) as caught:
        reprove_log_history_prewrite(evidence=_evidence(), token="secret-token")

    assert str(caught.value) == "trusted logs repository verification failed"
    assert "secret-token" not in str(caught.value)


def test_prewrite_rejects_forged_non_logs_evidence() -> None:
    evidence = _evidence()
    forged = TrustedLogHistoryEvidence(
        target=evidence.target,
        repository_id=evidence.repository_id,
        branch="main",
        expected_head_sha=evidence.expected_head_sha,
        commits=evidence.commits,
        plan=evidence.plan,
    )

    with pytest.raises(LogHistoryPrewriteError, match="restricted to the logs branch"):
        reprove_log_history_prewrite(evidence=forged, token="secret-token")


def test_prewrite_rejects_inconsistent_evidence_head() -> None:
    evidence = _evidence()
    forged = TrustedLogHistoryEvidence(
        target=evidence.target,
        repository_id=evidence.repository_id,
        branch=evidence.branch,
        expected_head_sha=_sha(9),
        commits=evidence.commits,
        plan=evidence.plan,
    )

    with pytest.raises(LogHistoryPrewriteError, match="head is inconsistent"):
        reprove_log_history_prewrite(evidence=forged, token="secret-token")
