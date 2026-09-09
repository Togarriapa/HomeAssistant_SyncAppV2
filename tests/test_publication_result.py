import pytest
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.publication_result import (
    PublicationResult,
    PublicationResultError,
    verify_publication_result,
)

LOCAL = "b" * 40
BASELINE = "a" * 40


def _normal_intent(**changes: object) -> PublicationIntent:
    values: dict[str, object] = {
        "target": "Owner/Home",
        "repository_id": 42,
        "branch": "main",
        "local_commit_sha": LOCAL,
        "expected_remote_commit_sha": BASELINE,
        "expect_remote_absent": False,
    }
    values.update(changes)
    return PublicationIntent(**values)  # type: ignore[arg-type]


def _initial_intent(**changes: object) -> PublicationIntent:
    values: dict[str, object] = {
        "target": "Owner/Home",
        "repository_id": 42,
        "branch": "main",
        "local_commit_sha": LOCAL,
        "expected_remote_commit_sha": None,
        "expect_remote_absent": True,
    }
    values.update(changes)
    return PublicationIntent(**values)  # type: ignore[arg-type]


def _remote(**changes: object) -> BranchHead:
    values: dict[str, object] = {
        "target": "Owner/Home",
        "repository_id": 42,
        "branch": "main",
        "commit_sha": LOCAL,
    }
    values.update(changes)
    return BranchHead(**values)  # type: ignore[arg-type]


def test_normal_publication_result_requires_exact_intended_remote_head() -> None:
    result = verify_publication_result(_normal_intent(), _remote())

    assert result == PublicationResult("Owner/Home", 42, "main", LOCAL)


def test_first_publication_result_uses_same_exact_post_transport_proof() -> None:
    result = verify_publication_result(_initial_intent(), _remote())

    assert result.commit_sha == LOCAL
    assert result.repository_id == 42


def test_repository_target_case_only_difference_is_accepted_and_canonicalized() -> None:
    result = verify_publication_result(
        _normal_intent(target="OWNER/HOME"),
        _remote(target="Owner/Home"),
    )

    assert result.target == "Owner/Home"


@pytest.mark.parametrize(
    ("remote", "message"),
    [
        (_remote(target="Other/Home"), "repository target"),
        (_remote(repository_id=99), "repository identity"),
        (_remote(branch="candidate"), "branch"),
        (_remote(commit_sha="c" * 40), "commit"),
    ],
)
def test_unexpected_trusted_remote_state_fails_closed(remote: BranchHead, message: str) -> None:
    with pytest.raises(PublicationResultError, match=message):
        verify_publication_result(_normal_intent(), remote)


@pytest.mark.parametrize(
    "intent",
    [
        _normal_intent(repository_id=0),
        _normal_intent(target="invalid"),
        _normal_intent(branch=""),
        _normal_intent(local_commit_sha="not-a-sha"),
        _normal_intent(expected_remote_commit_sha="not-a-sha"),
        _initial_intent(expected_remote_commit_sha=BASELINE),
    ],
)
def test_malformed_or_inconsistent_intent_fails_closed(intent: PublicationIntent) -> None:
    with pytest.raises(PublicationResultError):
        verify_publication_result(intent, _remote())


@pytest.mark.parametrize(
    "remote",
    [
        _remote(repository_id=0),
        _remote(target="invalid"),
        _remote(branch=""),
        _remote(commit_sha="not-a-sha"),
    ],
)
def test_malformed_trusted_remote_evidence_fails_closed(remote: BranchHead) -> None:
    with pytest.raises(PublicationResultError):
        verify_publication_result(_normal_intent(), remote)
