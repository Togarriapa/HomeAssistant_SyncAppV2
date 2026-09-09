"""Durable synchronization state promotion after trusted publication verification."""

from datetime import datetime

from ha_syncapp.git_workspace import GitWorkspace, verify_workspace_content
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.publication_result import PublicationResult, verify_publication_result
from ha_syncapp.state import StateError, StateStore, SynchronizationBaseline


class PublicationStateError(RuntimeError):
    """Verified publication evidence cannot safely advance durable state."""


def record_verified_publication(
    store: StateStore,
    workspace: GitWorkspace,
    intent: PublicationIntent,
    result: PublicationResult,
    *,
    synchronized_at: datetime | None = None,
) -> SynchronizationBaseline:
    """Advance the baseline only after remote success and local snapshot integrity are re-proven."""
    if type(store) is not StateStore:
        raise PublicationStateError("publication state store is invalid")
    if type(result) is not PublicationResult:
        raise PublicationStateError("publication result evidence is invalid")

    trusted_result = verify_publication_result(
        intent,
        BranchHead(
            target=result.target,
            repository_id=result.repository_id,
            branch=result.branch,
            commit_sha=result.commit_sha,
        ),
    )
    snapshot_id = verify_workspace_content(workspace)
    if snapshot_id != workspace.snapshot_id:
        raise PublicationStateError("publication workspace snapshot identity changed")

    try:
        pinned_repository_id = store.repository_id(trusted_result.target)
        if pinned_repository_id != trusted_result.repository_id:
            raise PublicationStateError("publication repository binding is missing or changed")
        baseline = store.record_synchronization_baseline(
            trusted_result.target,
            trusted_result.branch,
            snapshot_id,
            trusted_result.commit_sha,
            synchronized_at=synchronized_at,
        )
        persisted = store.synchronization_baseline(trusted_result.target, trusted_result.branch)
    except StateError as exc:
        raise PublicationStateError("verified publication state could not be persisted") from exc

    if persisted is None or persisted != baseline:
        raise PublicationStateError("verified publication baseline could not be re-proven")
    if (
        baseline.target != trusted_result.target
        or baseline.branch != trusted_result.branch
        or baseline.snapshot_id != snapshot_id
        or baseline.commit_sha != trusted_result.commit_sha
    ):
        raise PublicationStateError("persisted publication baseline does not match verified evidence")
    return baseline
