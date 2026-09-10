"""Read-only detection of one trusted Repo B candidate commit."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .github_repo import BranchAbsence, BranchHead, fetch_optional_trusted_branch_head

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CANDIDATE_BRANCH = "candidate"


class CandidateDetectionError(RuntimeError):
    """Candidate evidence is malformed or inconsistent."""


class CandidateDisposition(StrEnum):
    """Comparison result for one trusted candidate observation."""

    ABSENT = "absent"
    NEW = "new"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    """Identity-bound evidence for the exact trusted Repo B candidate head."""

    target: str
    repository_id: int
    branch: str
    commit_sha: str | None


def _validate_observation(observation: CandidateObservation) -> None:
    if (
        not isinstance(observation, CandidateObservation)
        or not isinstance(observation.target, str)
        or not observation.target
        or type(observation.repository_id) is not int
        or observation.repository_id <= 0
        or observation.branch != _CANDIDATE_BRANCH
        or (
            observation.commit_sha is not None
            and (
                not isinstance(observation.commit_sha, str)
                or _COMMIT_SHA.fullmatch(observation.commit_sha) is None
            )
        )
    ):
        raise CandidateDetectionError("Candidate observation is invalid")


def _validate_previous_sha(previous_sha: str | None) -> None:
    if previous_sha is not None and (
        not isinstance(previous_sha, str) or _COMMIT_SHA.fullmatch(previous_sha) is None
    ):
        raise CandidateDetectionError("Previous candidate commit is invalid")


def observe_trusted_candidate(
    target: str,
    token: str,
    *,
    expected_id: int,
) -> CandidateObservation:
    """Re-prove Repo B and observe only its exact ``candidate`` branch."""
    state = fetch_optional_trusted_branch_head(
        target,
        token,
        expected_id=expected_id,
        branch=_CANDIDATE_BRANCH,
    )
    if isinstance(state, BranchAbsence):
        observation = CandidateObservation(
            target=state.target,
            repository_id=state.repository_id,
            branch=state.branch,
            commit_sha=None,
        )
    elif isinstance(state, BranchHead):
        observation = CandidateObservation(
            target=state.target,
            repository_id=state.repository_id,
            branch=state.branch,
            commit_sha=state.commit_sha,
        )
    else:
        raise CandidateDetectionError("Candidate observation is invalid")
    _validate_observation(observation)
    return observation


def classify_candidate(
    observation: CandidateObservation,
    *,
    previous_sha: str | None,
) -> CandidateDisposition:
    """Classify trusted evidence without authorizing fetch, staging or deployment."""
    _validate_observation(observation)
    _validate_previous_sha(previous_sha)
    if observation.commit_sha is None:
        return CandidateDisposition.ABSENT
    if observation.commit_sha == previous_sha:
        return CandidateDisposition.UNCHANGED
    return CandidateDisposition.NEW
