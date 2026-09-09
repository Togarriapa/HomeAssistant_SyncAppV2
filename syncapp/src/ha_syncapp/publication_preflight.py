"""Side-effect-free conflict classification before any Repo B publication."""

import re
from dataclasses import dataclass
from enum import StrEnum

from ha_syncapp.github_repo import BranchAbsence, BranchHead
from ha_syncapp.state import SynchronizationBaseline

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class PublicationPreflightError(RuntimeError):
    """Publication evidence is malformed or does not describe the same target."""


class PublicationDisposition(StrEnum):
    """Explicit result of comparing local, baseline and trusted remote state."""

    SAFE_TO_PUBLISH = "safe_to_publish"
    SAFE_TO_INITIALIZE = "safe_to_initialize"
    NO_CHANGE = "no_change"
    ALREADY_PUBLISHED = "already_published"
    DIVERGED = "diverged"
    REMOTE_MISSING = "remote_missing"
    BASELINE_REQUIRED = "baseline_required"


@dataclass(frozen=True, slots=True)
class PublicationPreflight:
    disposition: PublicationDisposition
    target: str
    repository_id: int
    branch: str
    local_commit_sha: str
    remote_commit_sha: str | None
    baseline_commit_sha: str | None

    @property
    def may_publish(self) -> bool:
        """Return true only when later transport is explicitly authorized."""
        return self.disposition in {
            PublicationDisposition.SAFE_TO_PUBLISH,
            PublicationDisposition.SAFE_TO_INITIALIZE,
        }

    @property
    def requires_initialization(self) -> bool:
        """Return true only for an explicitly absent first-publication branch."""
        return self.disposition is PublicationDisposition.SAFE_TO_INITIALIZE


def assess_publication_preflight(
    local_commit_sha: str,
    remote: BranchHead | BranchAbsence,
    baseline: SynchronizationBaseline | None,
) -> PublicationPreflight:
    """Classify publication safety without performing Git or network operations."""
    if not isinstance(local_commit_sha, str) or _COMMIT_SHA.fullmatch(local_commit_sha) is None:
        raise PublicationPreflightError("local commit identity is invalid")
    _validate_remote_evidence(remote)

    if baseline is None:
        disposition = (
            PublicationDisposition.SAFE_TO_INITIALIZE
            if isinstance(remote, BranchAbsence)
            else PublicationDisposition.BASELINE_REQUIRED
        )
        return PublicationPreflight(
            disposition=disposition,
            target=remote.target,
            repository_id=remote.repository_id,
            branch=remote.branch,
            local_commit_sha=local_commit_sha,
            remote_commit_sha=_remote_commit_sha(remote),
            baseline_commit_sha=None,
        )

    if type(baseline) is not SynchronizationBaseline:
        raise PublicationPreflightError("synchronization baseline evidence is invalid")
    if baseline.target.casefold() != remote.target.casefold() or baseline.branch != remote.branch:
        raise PublicationPreflightError(
            "publication evidence does not describe one repository branch"
        )
    if _COMMIT_SHA.fullmatch(baseline.commit_sha) is None:
        raise PublicationPreflightError("baseline commit identity is invalid")

    if isinstance(remote, BranchAbsence):
        return PublicationPreflight(
            disposition=PublicationDisposition.REMOTE_MISSING,
            target=remote.target,
            repository_id=remote.repository_id,
            branch=remote.branch,
            local_commit_sha=local_commit_sha,
            remote_commit_sha=None,
            baseline_commit_sha=baseline.commit_sha,
        )

    if remote.commit_sha == baseline.commit_sha:
        disposition = (
            PublicationDisposition.NO_CHANGE
            if local_commit_sha == remote.commit_sha
            else PublicationDisposition.SAFE_TO_PUBLISH
        )
    elif remote.commit_sha == local_commit_sha:
        disposition = PublicationDisposition.ALREADY_PUBLISHED
    else:
        disposition = PublicationDisposition.DIVERGED

    return PublicationPreflight(
        disposition=disposition,
        target=remote.target,
        repository_id=remote.repository_id,
        branch=remote.branch,
        local_commit_sha=local_commit_sha,
        remote_commit_sha=remote.commit_sha,
        baseline_commit_sha=baseline.commit_sha,
    )


def _validate_remote_evidence(remote: BranchHead | BranchAbsence) -> None:
    if not isinstance(remote, (BranchHead, BranchAbsence)):
        raise PublicationPreflightError("trusted remote branch evidence is invalid")
    if type(remote.repository_id) is not int or remote.repository_id <= 0:
        raise PublicationPreflightError("trusted remote repository identity is invalid")
    if not isinstance(remote.target, str) or len(remote.target.split("/")) != 2:
        raise PublicationPreflightError("trusted remote repository target is invalid")
    if not all(remote.target.split("/")) or not isinstance(remote.branch, str) or not remote.branch:
        raise PublicationPreflightError("trusted remote branch identity is invalid")
    if isinstance(remote, BranchHead) and _COMMIT_SHA.fullmatch(remote.commit_sha) is None:
        raise PublicationPreflightError("trusted remote commit identity is invalid")


def _remote_commit_sha(remote: BranchHead | BranchAbsence) -> str | None:
    if isinstance(remote, BranchHead):
        return remote.commit_sha
    return None
