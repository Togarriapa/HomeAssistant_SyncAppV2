"""Side-effect-free verification of trusted Repo B publication results."""

import re
from dataclasses import dataclass

from ha_syncapp.github_repo import BranchHead
from ha_syncapp.publication_intent import PublicationIntent

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class PublicationResultError(RuntimeError):
    """Trusted post-publication evidence does not prove the intended result."""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    target: str
    repository_id: int
    branch: str
    commit_sha: str


def verify_publication_result(
    intent: PublicationIntent,
    remote: BranchHead,
) -> PublicationResult:
    """Prove the trusted remote branch now points at the exact intended local commit."""
    _validate_intent(intent)
    _validate_remote(remote)
    if intent.target.casefold() != remote.target.casefold():
        raise PublicationResultError("publication result repository target does not match intent")
    if intent.repository_id != remote.repository_id:
        raise PublicationResultError("publication result repository identity does not match intent")
    if intent.branch != remote.branch:
        raise PublicationResultError("publication result branch does not match intent")
    if intent.local_commit_sha != remote.commit_sha:
        raise PublicationResultError("publication result commit does not match intended commit")
    return PublicationResult(
        target=remote.target,
        repository_id=remote.repository_id,
        branch=remote.branch,
        commit_sha=remote.commit_sha,
    )


def _validate_intent(intent: PublicationIntent) -> None:
    if type(intent) is not PublicationIntent:
        raise PublicationResultError("publication intent evidence is invalid")
    if type(intent.repository_id) is not int or intent.repository_id <= 0:
        raise PublicationResultError("publication intent repository identity is invalid")
    if not isinstance(intent.target, str):
        raise PublicationResultError("publication intent repository target is invalid")
    parts = intent.target.split("/")
    if len(parts) != 2 or not all(parts):
        raise PublicationResultError("publication intent repository target is invalid")
    if not isinstance(intent.branch, str) or not intent.branch:
        raise PublicationResultError("publication intent branch is invalid")
    if _COMMIT_SHA.fullmatch(intent.local_commit_sha) is None:
        raise PublicationResultError("publication intent commit identity is invalid")
    if intent.expect_remote_absent:
        if intent.expected_remote_commit_sha is not None:
            raise PublicationResultError(
                "publication initialization intent is internally inconsistent"
            )
    else:
        if (
            intent.expected_remote_commit_sha is None
            or _COMMIT_SHA.fullmatch(intent.expected_remote_commit_sha) is None
        ):
            raise PublicationResultError("publication intent remote expectation is invalid")


def _validate_remote(remote: BranchHead) -> None:
    if type(remote) is not BranchHead:
        raise PublicationResultError("trusted publication result evidence is invalid")
    if type(remote.repository_id) is not int or remote.repository_id <= 0:
        raise PublicationResultError("trusted publication repository identity is invalid")
    if not isinstance(remote.target, str):
        raise PublicationResultError("trusted publication repository target is invalid")
    parts = remote.target.split("/")
    if len(parts) != 2 or not all(parts):
        raise PublicationResultError("trusted publication repository target is invalid")
    if not isinstance(remote.branch, str) or not remote.branch:
        raise PublicationResultError("trusted publication branch is invalid")
    if _COMMIT_SHA.fullmatch(remote.commit_sha) is None:
        raise PublicationResultError("trusted publication commit identity is invalid")
