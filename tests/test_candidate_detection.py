from pathlib import Path

import pytest
from ha_syncapp.candidate_detection import (
    CandidateDetectionError,
    CandidateDisposition,
    CandidateObservation,
    classify_candidate,
    detect_and_enqueue_trusted_candidate,
    observe_trusted_candidate,
)
from ha_syncapp.github_repo import BranchAbsence, BranchHead, RepositoryVerificationError
from ha_syncapp.state import StateStore

SHA_A = "a" * 40
SHA_B = "b" * 40
TARGET = "Owner/Home"
REPOSITORY_ID = 42


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _absent_candidate(
    target: str,
    token: str,
    *,
    expected_id: int,
    branch: str,
) -> BranchAbsence:
    del target, token, expected_id, branch
    return BranchAbsence(TARGET, REPOSITORY_ID, "candidate")


def _wrong_branch(
    target: str,
    token: str,
    *,
    expected_id: int,
    branch: str,
) -> BranchHead:
    del target, token, expected_id, branch
    return BranchHead(TARGET, REPOSITORY_ID, "main", SHA_A)


def test_observes_trusted_candidate_head(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_fetch(target: str, token: str, *, expected_id: int, branch: str) -> BranchHead:
        captured.update(
            target=target,
            token=token,
            expected_id=expected_id,
            branch=branch,
        )
        return BranchHead(TARGET, REPOSITORY_ID, "candidate", SHA_A)

    monkeypatch.setattr(
        "ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head",
        fake_fetch,
    )
    observed = observe_trusted_candidate(TARGET, "secret-sentinel", expected_id=REPOSITORY_ID)

    assert observed == CandidateObservation(TARGET, REPOSITORY_ID, "candidate", SHA_A)
    assert captured == {
        "target": TARGET,
        "token": "secret-sentinel",
        "expected_id": REPOSITORY_ID,
        "branch": "candidate",
    }


def test_observes_absent_candidate_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head",
        _absent_candidate,
    )
    observed = observe_trusted_candidate(TARGET, "token", expected_id=REPOSITORY_ID)
    assert observed.commit_sha is None
    assert classify_candidate(observed, previous_sha=None) is CandidateDisposition.ABSENT


def test_classifies_new_and_unchanged_candidate() -> None:
    observed = CandidateObservation(TARGET, REPOSITORY_ID, "candidate", SHA_A)
    assert classify_candidate(observed, previous_sha=None) is CandidateDisposition.NEW
    assert classify_candidate(observed, previous_sha=SHA_B) is CandidateDisposition.NEW
    assert classify_candidate(observed, previous_sha=SHA_A) is CandidateDisposition.UNCHANGED


def test_detection_requires_existing_repository_binding_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    called = False

    def fetch(*args: object, **kwargs: object) -> BranchHead:
        nonlocal called
        called = True
        return BranchHead(TARGET, REPOSITORY_ID, "candidate", SHA_A)

    monkeypatch.setattr("ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head", fetch)
    try:
        with pytest.raises(CandidateDetectionError, match="binding is unavailable"):
            detect_and_enqueue_trusted_candidate(store, TARGET, "token")
    finally:
        store.__exit__(None, None, None)

    assert called is False


def test_detection_enqueues_exact_trusted_sha_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.bind_repository(TARGET, REPOSITORY_ID)
    observations = 0

    def fetch(target: str, token: str, *, expected_id: int, branch: str) -> BranchHead:
        nonlocal observations
        observations += 1
        assert (target, expected_id, branch) == (TARGET, REPOSITORY_ID, "candidate")
        return BranchHead(TARGET, REPOSITORY_ID, "candidate", SHA_A)

    monkeypatch.setattr("ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head", fetch)
    try:
        first = detect_and_enqueue_trusted_candidate(store, TARGET, "token")
        second = detect_and_enqueue_trusted_candidate(store, TARGET, "token")
        claimed = store.claim_work_kind("candidate")
        no_duplicate = store.claim_work_kind("candidate")
    finally:
        store.__exit__(None, None, None)

    assert observations == 2
    assert first.observation.commit_sha == SHA_A
    assert first.work is not None and first.work.work_key == SHA_A
    assert first.work.status == "pending"
    assert second.work == first.work
    assert claimed is not None and claimed.work_key == SHA_A
    assert no_duplicate is None


def test_new_candidate_sha_creates_distinct_pending_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.bind_repository(TARGET, REPOSITORY_ID)
    heads = iter((SHA_A, SHA_B))

    def fetch(target: str, token: str, *, expected_id: int, branch: str) -> BranchHead:
        return BranchHead(TARGET, REPOSITORY_ID, "candidate", next(heads))

    monkeypatch.setattr("ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head", fetch)
    try:
        first = detect_and_enqueue_trusted_candidate(store, TARGET, "token")
        second = detect_and_enqueue_trusted_candidate(store, TARGET, "token")
        claimed_first = store.claim_work_kind("candidate")
        claimed_second = store.claim_work_kind("candidate")
    finally:
        store.__exit__(None, None, None)

    assert first.work is not None and first.work.work_key == SHA_A
    assert second.work is not None and second.work.work_key == SHA_B
    assert claimed_first is not None
    assert claimed_second is not None
    assert {claimed_first.work_key, claimed_second.work_key} == {SHA_A, SHA_B}


def test_absent_candidate_is_clean_noop_and_enqueues_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.bind_repository(TARGET, REPOSITORY_ID)
    monkeypatch.setattr(
        "ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head",
        _absent_candidate,
    )
    try:
        result = detect_and_enqueue_trusted_candidate(store, TARGET, "token")
        claimed = store.claim_work_kind("candidate")
    finally:
        store.__exit__(None, None, None)

    assert result.observation.commit_sha is None
    assert result.work is None
    assert claimed is None


def test_repository_or_transport_failure_enqueues_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.bind_repository(TARGET, REPOSITORY_ID)

    def fail(*args: object, **kwargs: object) -> BranchHead:
        raise RepositoryVerificationError("GitHub repository verification transport failed")

    monkeypatch.setattr("ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head", fail)
    try:
        with pytest.raises(RepositoryVerificationError, match="transport failed"):
            detect_and_enqueue_trusted_candidate(store, TARGET, "secret-sentinel")
        claimed = store.claim_work_kind("candidate")
    finally:
        store.__exit__(None, None, None)

    assert claimed is None


@pytest.mark.parametrize("previous_sha", ["", "not-a-sha", "A" * 40, "a" * 39, True])
def test_rejects_invalid_previous_sha(previous_sha: object) -> None:
    observed = CandidateObservation(TARGET, REPOSITORY_ID, "candidate", SHA_A)
    with pytest.raises(CandidateDetectionError, match="Previous candidate commit is invalid"):
        classify_candidate(observed, previous_sha=previous_sha)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "observed",
    [
        CandidateObservation(TARGET, REPOSITORY_ID, "main", SHA_A),
        CandidateObservation(TARGET, 0, "candidate", SHA_A),
        CandidateObservation("", REPOSITORY_ID, "candidate", SHA_A),
        CandidateObservation(TARGET, REPOSITORY_ID, "candidate", "bad"),
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
        observe_trusted_candidate(TARGET, "secret-sentinel", expected_id=REPOSITORY_ID)


def test_rejects_unexpected_trusted_branch_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_syncapp.candidate_detection.fetch_optional_trusted_branch_head",
        _wrong_branch,
    )
    with pytest.raises(CandidateDetectionError, match="Candidate observation is invalid"):
        observe_trusted_candidate(TARGET, "token", expected_id=REPOSITORY_ID)
