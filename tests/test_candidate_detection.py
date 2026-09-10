import pytest
from ha_syncapp.candidate_detection import (
    CandidateDetectionError,
    CandidateDisposition,
    CandidateObservation,
    classify_candidate,
    observe_trusted_candidate,
)
from ha_syncapp.github_repo import BranchAbsence, BranchHead, RepositoryVerificationError

SHA_A = "a" * 40
SHA_B = "b" * 40


def test_observes_trusted_candidate_head(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_fetch(target: str, token: str, *, expected_id: int, branch: str) -> BranchHead:
        captured.update(
            target=target,
            token=token,
            expected_id=expected_id,
            branch=branch,
        )
        return BranchHead("Owner/Home", 42, "candidate", SHA_A)

    monkeypatch.setattr(
        "ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head",
        fake_fetch,
    )
    observed = observe_trusted_candidate("Owner/Home", "secret-sentinel", expected_id=42)

    assert observed == CandidateObservation("Owner/Home", 42, "candidate", SHA_A)
    assert captured == {
        "target": "Owner/Home",
        "token": "secret-sentinel",
        "expected_id": 42,
        "branch": "candidate",
    }


def test_observes_absent_candidate_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head",
        lambda target, token, *, expected_id, branch: BranchAbsence(
            "Owner/Home", 42, "candidate"
        ),
    )
    observed = observe_trusted_candidate("Owner/Home", "token", expected_id=42)
    assert observed.commit_sha is None
    assert classify_candidate(observed, previous_sha=None) is CandidateDisposition.ABSENT


def test_classifies_new_and_unchanged_candidate() -> None:
    observed = CandidateObservation("Owner/Home", 42, "candidate", SHA_A)
    assert classify_candidate(observed, previous_sha=None) is CandidateDisposition.NEW
    assert classify_candidate(observed, previous_sha=SHA_B) is CandidateDisposition.NEW
    assert classify_candidate(observed, previous_sha=SHA_A) is CandidateDisposition.UNCHANGED


@pytest.mark.parametrize("previous_sha", ["", "not-a-sha", "A" * 40, "a" * 39, True])
def test_rejects_invalid_previous_sha(previous_sha: object) -> None:
    observed = CandidateObservation("Owner/Home", 42, "candidate", SHA_A)
    with pytest.raises(CandidateDetectionError, match="Previous candidate commit is invalid"):
        classify_candidate(observed, previous_sha=previous_sha)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "observed",
    [
        CandidateObservation("Owner/Home", 42, "main", SHA_A),
        CandidateObservation("Owner/Home", 0, "candidate", SHA_A),
        CandidateObservation("", 42, "candidate", SHA_A),
        CandidateObservation("Owner/Home", 42, "candidate", "bad"),
    ],
)
def test_rejects_malformed_or_non_candidate_evidence(observed: CandidateObservation) -> None:
    with pytest.raises(CandidateDetectionError, match="Candidate observation is invalid"):
        classify_candidate(observed, previous_sha=None)


def test_repository_or_transport_failure_is_not_branch_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> BranchHead:
        raise RepositoryVerificationError("GitHub repository verification transport failed")

    monkeypatch.setattr("ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head", fail)
    with pytest.raises(RepositoryVerificationError, match="transport failed"):
        observe_trusted_candidate("Owner/Home", "secret-sentinel", expected_id=42)


def test_rejects_unexpected_trusted_branch_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head",
        lambda target, token, *, expected_id, branch: BranchHead(
            "Owner/Home", 42, "main", SHA_A
        ),
    )
    with pytest.raises(CandidateDetectionError, match="Candidate observation is invalid"):
        observe_trusted_candidate("Owner/Home", "token", expected_id=42)
