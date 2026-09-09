"""Immutable compare-and-swap intent produced before any Repo B publication transport."""

import re
from dataclasses import dataclass

from ha_syncapp.git_workspace import GitWorkspace
from ha_syncapp.local_git import verify_fast_forward_ancestry
from ha_syncapp.publication_preflight import (
    PublicationDisposition,
    PublicationPreflight,
)

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class PublicationIntentError(RuntimeError):
    """Publication preflight evidence cannot safely authorize transport."""


@dataclass(frozen=True, slots=True)
class PublicationIntent:
    target: str
    repository_id: int
    branch: str
    local_commit_sha: str
    expected_remote_commit_sha: str | None
    expect_remote_absent: bool


def build_publication_intent(
    workspace: GitWorkspace,
    preflight: PublicationPreflight,
) -> PublicationIntent:
    """Bind an authorized preflight to exact local and expected remote Git state."""
    if type(preflight) is not PublicationPreflight:
        raise PublicationIntentError("publication preflight evidence is invalid")
    _validate_common(preflight)

    if preflight.disposition is PublicationDisposition.SAFE_TO_PUBLISH:
        if preflight.requires_initialization or not preflight.may_publish:
            raise PublicationIntentError("normal publication preflight is internally inconsistent")
        if preflight.remote_commit_sha is None or preflight.baseline_commit_sha is None:
            raise PublicationIntentError("normal publication requires an exact remote baseline")
        if preflight.remote_commit_sha != preflight.baseline_commit_sha:
            raise PublicationIntentError("normal publication remote state does not match baseline")
        verify_fast_forward_ancestry(
            workspace,
            baseline_commit_sha=preflight.baseline_commit_sha,
            local_commit_sha=preflight.local_commit_sha,
        )
        return PublicationIntent(
            target=preflight.target,
            repository_id=preflight.repository_id,
            branch=preflight.branch,
            local_commit_sha=preflight.local_commit_sha,
            expected_remote_commit_sha=preflight.remote_commit_sha,
            expect_remote_absent=False,
        )

    if preflight.disposition is PublicationDisposition.SAFE_TO_INITIALIZE:
        if not preflight.requires_initialization or not preflight.may_publish:
            raise PublicationIntentError("initial publication preflight is internally inconsistent")
        if preflight.remote_commit_sha is not None or preflight.baseline_commit_sha is not None:
            raise PublicationIntentError("initial publication requires an absent remote with no baseline")
        verify_fast_forward_ancestry(
            workspace,
            baseline_commit_sha=preflight.local_commit_sha,
            local_commit_sha=preflight.local_commit_sha,
        )
        return PublicationIntent(
            target=preflight.target,
            repository_id=preflight.repository_id,
            branch=preflight.branch,
            local_commit_sha=preflight.local_commit_sha,
            expected_remote_commit_sha=None,
            expect_remote_absent=True,
        )

    raise PublicationIntentError("publication preflight does not authorize transport")


def _validate_common(preflight: PublicationPreflight) -> None:
    if type(preflight.repository_id) is not int or preflight.repository_id <= 0:
        raise PublicationIntentError("publication repository identity is invalid")
    target_parts = preflight.target.split("/") if isinstance(preflight.target, str) else []
    if len(target_parts) != 2 or not all(target_parts):
        raise PublicationIntentError("publication repository target is invalid")
    if not isinstance(preflight.branch, str) or not preflight.branch:
        raise PublicationIntentError("publication branch identity is invalid")
    if _COMMIT_SHA.fullmatch(preflight.local_commit_sha) is None:
        raise PublicationIntentError("publication local commit identity is invalid")
    for commit_sha in (preflight.remote_commit_sha, preflight.baseline_commit_sha):
        if commit_sha is not None and _COMMIT_SHA.fullmatch(commit_sha) is None:
            raise PublicationIntentError("publication remote commit evidence is invalid")
