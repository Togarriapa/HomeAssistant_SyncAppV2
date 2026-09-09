"""Side-effect-free conflict classification before any Repo B publication."""

import re
from dataclasses import dataclass
from enum import StrEnum

from ha_syncapp.github_repo import BranchHead
from ha_syncapp.state import SynchronizationBaseline

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class PublicationPreflightError(RuntimeError):
    """Publication evidence is malformed or does not describe the same target."""


class PublicationDisposition(StrEnum):
    """Explicit result of comparing local, baseline and trusted remote state."""

    SAFE_TO_PUBLISH = "safe_to_publish"
    NO_CHANGE = "no_change"
    ALREADY_PUBLISHED = "already_published"
    DIVERGED = "diverged"
    BASELINE_REQUIRED = "baseline_required"


@dataclass(frozen=True, slots=True)
class PublicationPreflight:
    disposition: PublicationDisposition
    target: str
    branch: str
    local_commit_sha: str
    remote_commit_sha: str
    baseline_commit_sha: str | None

    @property
    def may_publish(self) -> bool:
        """Return true only for the one disposition that authorizes a later push attempt."""
        return self.disposition is PublicationDisposition.SAFE_TO_PUBLISH


def assess_publication_preflight(
    local_commit_sha: str,
    remote: BranchHead,
    baseline: SynchronizationBaseline | None,
) -> PublicationPreflight:
    """Classify publication safety without performing Git or network operations."""
    if not isinstance(local_commit_sha, str) or _COMMIT_SHA.fullmatch(local_commit_sha) is None:
        raise PublicationPreflightError("local commit identity is invalid")
    if type(remote) is not BranchHead:
        raise PublicationPreflightError("trusted remote branch evidence is invalid")
    if _COMMIT_SHA.fullmatch(remote.commit_sha) is None:
        raise PublicationPreflightError("trusted remote commit identity is invalid")

    if baseline is None:
        return PublicationPreflight(
            disposition=PublicationDisposition.BASELINE_REQUIRED,
            target=remote.target,
            branch=remote.branch,
            local_commit_sha=local_commit_sha,
            remote_commit_sha=remote.commit_sha,
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
        branch=remote.branch,
        local_commit_sha=local_commit_sha,
        remote_commit_sha=remote.commit_sha,
        baseline_commit_sha=baseline.commit_sha,
    )
