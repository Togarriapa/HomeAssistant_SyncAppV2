"""Crash-safe recovery when Repo B already contains the intended local commit."""

import re
from dataclasses import dataclass
from datetime import datetime

from ha_syncapp.git_workspace import GitWorkspace, WorkspaceError, verify_workspace_content
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.publication_preflight import (
    PublicationDisposition,
    PublicationPreflight,
)
from ha_syncapp.state import StateError, StateStore, SynchronizationBaseline

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class PublicationRecoveryError(RuntimeError):
    """Interrupted publication evidence cannot safely complete recovery."""


@dataclass(frozen=True, slots=True)
class PublicationRecoveryIntent:
    target: str
    repository_id: int
    branch: str
    local_commit_sha: str
    prior_baseline_commit_sha: str


def build_publication_recovery_intent(
    preflight: PublicationPreflight,
) -> PublicationRecoveryIntent:
    """Bind an ALREADY_PUBLISHED preflight to immutable recovery evidence."""
    if type(preflight) is not PublicationPreflight:
        raise PublicationRecoveryError("publication recovery preflight evidence is invalid")
    if preflight.disposition is not PublicationDisposition.ALREADY_PUBLISHED:
        raise PublicationRecoveryError("publication preflight does not authorize recovery")
    if preflight.may_publish or preflight.requires_initialization:
        raise PublicationRecoveryError("publication recovery preflight is internally inconsistent")
    if type(preflight.repository_id) is not int or preflight.repository_id <= 0:
        raise PublicationRecoveryError("publication recovery repository identity is invalid")
    if not _valid_target(preflight.target) or not isinstance(preflight.branch, str) or not preflight.branch:
        raise PublicationRecoveryError("publication recovery branch identity is invalid")
    if _COMMIT_SHA.fullmatch(preflight.local_commit_sha) is None:
        raise PublicationRecoveryError("publication recovery local commit identity is invalid")
    if preflight.remote_commit_sha != preflight.local_commit_sha:
        raise PublicationRecoveryError("publication recovery remote state does not match local commit")
    if (
        preflight.baseline_commit_sha is None
        or _COMMIT_SHA.fullmatch(preflight.baseline_commit_sha) is None
        or preflight.baseline_commit_sha == preflight.local_commit_sha
    ):
        raise PublicationRecoveryError("publication recovery prior baseline is invalid")
    return PublicationRecoveryIntent(
        target=preflight.target,
        repository_id=preflight.repository_id,
        branch=preflight.branch,
        local_commit_sha=preflight.local_commit_sha,
        prior_baseline_commit_sha=preflight.baseline_commit_sha,
    )


def complete_publication_recovery(
    store: StateStore,
    workspace: GitWorkspace,
    intent: PublicationRecoveryIntent,
    remote: BranchHead,
    *,
    synchronized_at: datetime | None = None,
) -> SynchronizationBaseline:
    """Record the already-published commit only after fresh remote and local proofs."""
    if type(store) is not StateStore:
        raise PublicationRecoveryError("publication recovery state store is invalid")
    _validate_recovery_intent(intent)
    _validate_fresh_remote(intent, remote)
    try:
        snapshot_id = verify_workspace_content(workspace)
    except WorkspaceError as exc:
        raise PublicationRecoveryError("publication recovery workspace could not be re-proven") from exc
    if snapshot_id != workspace.snapshot_id:
        raise PublicationRecoveryError("publication recovery snapshot identity changed")

    try:
        current = store.synchronization_baseline(intent.target, intent.branch)
        if current is None or current.commit_sha != intent.prior_baseline_commit_sha:
            if current is not None and current.commit_sha == intent.local_commit_sha:
                return current
            raise PublicationRecoveryError("publication recovery baseline changed unexpectedly")
        if store.repository_id(intent.target) != intent.repository_id:
            raise PublicationRecoveryError("publication recovery repository binding changed")
        recorded = store.record_synchronization_baseline(
            intent.target,
            intent.branch,
            snapshot_id,
            intent.local_commit_sha,
            synchronized_at=synchronized_at,
        )
        persisted = store.synchronization_baseline(intent.target, intent.branch)
    except StateError as exc:
        raise PublicationRecoveryError("publication recovery state could not be persisted") from exc
    if persisted != recorded:
        raise PublicationRecoveryError("publication recovery baseline could not be re-proven")
    return recorded


def _validate_recovery_intent(intent: PublicationRecoveryIntent) -> None:
    if type(intent) is not PublicationRecoveryIntent:
        raise PublicationRecoveryError("publication recovery intent evidence is invalid")
    if type(intent.repository_id) is not int or intent.repository_id <= 0:
        raise PublicationRecoveryError("publication recovery repository identity is invalid")
    if not _valid_target(intent.target) or not isinstance(intent.branch, str) or not intent.branch:
        raise PublicationRecoveryError("publication recovery branch identity is invalid")
    if (
        _COMMIT_SHA.fullmatch(intent.local_commit_sha) is None
        or _COMMIT_SHA.fullmatch(intent.prior_baseline_commit_sha) is None
        or intent.local_commit_sha == intent.prior_baseline_commit_sha
    ):
        raise PublicationRecoveryError("publication recovery commit evidence is invalid")


def _validate_fresh_remote(intent: PublicationRecoveryIntent, remote: BranchHead) -> None:
    if type(remote) is not BranchHead:
        raise PublicationRecoveryError("trusted publication recovery remote evidence is invalid")
    if intent.target.casefold() != remote.target.casefold():
        raise PublicationRecoveryError("publication recovery repository target changed")
    if intent.repository_id != remote.repository_id:
        raise PublicationRecoveryError("publication recovery repository identity changed")
    if intent.branch != remote.branch:
        raise PublicationRecoveryError("publication recovery branch changed")
    if intent.local_commit_sha != remote.commit_sha:
        raise PublicationRecoveryError("publication recovery remote commit changed")


def _valid_target(target: object) -> bool:
    if not isinstance(target, str):
        return False
    parts = target.split("/")
    return len(parts) == 2 and all(parts)
