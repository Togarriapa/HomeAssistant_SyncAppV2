from datetime import UTC, datetime

import pytest
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.publication_preflight import (
    PublicationDisposition,
    PublicationPreflightError,
    assess_publication_preflight,
)
from ha_syncapp.state import SynchronizationBaseline


LOCAL = "b" * 40
BASELINE = "a" * 40
REMOTE_OTHER = "c" * 40


def _baseline(
    commit_sha: str = BASELINE, *, target: str = "Owner/Home", branch: str = "main"
) -> SynchronizationBaseline:
    return SynchronizationBaseline(
        target=target,
        branch=branch,
        snapshot_id="d" * 64,
        commit_sha=commit_sha,
        synchronized_at=datetime(2026, 9, 9, tzinfo=UTC),
    )


def _remote(
    commit_sha: str = BASELINE, *, target: str = "Owner/Home", branch: str = "main"
) -> BranchHead:
    return BranchHead(target=target, repository_id=42, branch=branch, commit_sha=commit_sha)


def test_unchanged_remote_baseline_allows_later_publication() -> None:
    result = assess_publication_preflight(LOCAL, _remote(), _baseline())

    assert result.disposition is PublicationDisposition.SAFE_TO_PUBLISH
    assert result.may_publish is True
    assert result.baseline_commit_sha == BASELINE


def test_equal_local_remote_and_baseline_is_explicit_no_change() -> None:
    result = assess_publication_preflight(BASELINE, _remote(), _baseline())

    assert result.disposition is PublicationDisposition.NO_CHANGE
    assert result.may_publish is False


def test_remote_equal_to_local_with_older_baseline_is_recoverable_already_published() -> None:
    result = assess_publication_preflight(LOCAL, _remote(LOCAL), _baseline())

    assert result.disposition is PublicationDisposition.ALREADY_PUBLISHED
    assert result.may_publish is False


def test_unrelated_remote_change_is_divergence_and_never_authorizes_publication() -> None:
    result = assess_publication_preflight(LOCAL, _remote(REMOTE_OTHER), _baseline())

    assert result.disposition is PublicationDisposition.DIVERGED
    assert result.may_publish is False


def test_existing_remote_without_baseline_fails_closed_as_baseline_required() -> None:
    result = assess_publication_preflight(LOCAL, _remote(REMOTE_OTHER), None)

    assert result.disposition is PublicationDisposition.BASELINE_REQUIRED
    assert result.may_publish is False
    assert result.baseline_commit_sha is None


@pytest.mark.parametrize(
    ("baseline", "remote"),
    [
        (_baseline(target="Other/Home"), _remote()),
        (_baseline(branch="candidate"), _remote()),
    ],
)
def test_mismatched_repository_or_branch_evidence_is_rejected(
    baseline: SynchronizationBaseline, remote: BranchHead
) -> None:
    with pytest.raises(PublicationPreflightError):
        assess_publication_preflight(LOCAL, remote, baseline)


@pytest.mark.parametrize("commit_sha", ["", "not-a-sha", "A" * 40, "a" * 39])
def test_invalid_local_commit_identity_is_rejected(commit_sha: str) -> None:
    with pytest.raises(PublicationPreflightError):
        assess_publication_preflight(commit_sha, _remote(), _baseline())


def test_case_only_repository_target_difference_preserves_bound_identity() -> None:
    result = assess_publication_preflight(
        LOCAL,
        _remote(target="owner/home"),
        _baseline(target="Owner/Home"),
    )

    assert result.disposition is PublicationDisposition.SAFE_TO_PUBLISH
    assert result.target == "owner/home"
