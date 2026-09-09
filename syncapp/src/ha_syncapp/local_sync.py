"""One fail-closed Local -> Repo B synchronization cycle from a read-only source."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ha_syncapp.baseline_anchor import BaselineAnchorError, anchor_trusted_baseline
from ha_syncapp.git_workspace import GitWorkspace, WorkspaceError, prepare_git_workspace
from ha_syncapp.github_repo import (
    BranchAbsence,
    BranchHead,
    RepositoryVerificationError,
    fetch_optional_trusted_branch_head,
)
from ha_syncapp.local_git import GitError, create_snapshot_commit, initialize_repository
from ha_syncapp.main_routing import include_in_main
from ha_syncapp.publication_intent import PublicationIntentError, build_publication_intent
from ha_syncapp.publication_preflight import (
    PublicationDisposition,
    PublicationPreflightError,
    assess_publication_preflight,
)
from ha_syncapp.publication_workflow import (
    PublicationWorkflowError,
    complete_authorized_publication,
)
from ha_syncapp.snapshot import Snapshot, SnapshotError
from ha_syncapp.snapshot import capture_snapshot as _capture_snapshot
from ha_syncapp.state import StateError, StateStore, SynchronizationBaseline


class LocalSyncError(RuntimeError):
    """A local synchronization cycle could not complete safely."""


class LocalSyncDisposition(StrEnum):
    """Externally useful result of one guarded synchronization cycle."""

    INITIALIZED = "initialized"
    PUBLISHED = "published"
    NO_CHANGE = "no_change"
    BASELINE_REQUIRED = "baseline_required"
    DIVERGED = "diverged"
    REMOTE_MISSING = "remote_missing"


@dataclass(frozen=True, slots=True)
class LocalSyncResult:
    disposition: LocalSyncDisposition
    target: str
    repository_id: int
    branch: str
    snapshot_id: str
    commit_sha: str | None
    baseline: SynchronizationBaseline | None


def capture_snapshot(source: Path, staging_root: Path) -> Snapshot:
    """Capture only files explicitly routed to Repo B main."""
    return _capture_snapshot(source, staging_root, include_path=include_in_main)


def synchronize_local_configuration(
    store: StateStore,
    source: Path,
    snapshot_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
    *,
    branch: str = "main",
) -> LocalSyncResult:
    """Capture, classify and when authorized publish one stable local configuration snapshot."""
    if type(store) is not StateStore:
        raise LocalSyncError("local synchronization state store is invalid")
    if branch != "main":
        raise LocalSyncError("local configuration synchronization only supports main")

    snapshot: Snapshot | None = None
    workspace: GitWorkspace | None = None
    try:
        repository_id = store.repository_id(target)
        if repository_id is None:
            raise LocalSyncError("local synchronization repository is not pinned")

        snapshot = capture_snapshot(source, snapshot_root)
        workspace = prepare_git_workspace(snapshot.root, workspace_root)
        initialize_repository(workspace, default_branch=branch)

        remote = fetch_optional_trusted_branch_head(
            target,
            token,
            expected_id=repository_id,
            branch=branch,
        )
        baseline = store.synchronization_baseline(target, branch)

        refusal = _classify_pre_commit_refusal(
            target,
            repository_id,
            branch,
            snapshot,
            remote,
            baseline,
        )
        if refusal is not None:
            return refusal

        if isinstance(remote, BranchHead):
            anchor_trusted_baseline(workspace, remote, token)

        local_commit_sha = create_snapshot_commit(workspace)
        if local_commit_sha is None:
            if not isinstance(remote, BranchHead) or baseline is None:
                raise LocalSyncError("initial synchronization produced no publishable commit")
            return LocalSyncResult(
                disposition=LocalSyncDisposition.NO_CHANGE,
                target=remote.target,
                repository_id=remote.repository_id,
                branch=remote.branch,
                snapshot_id=snapshot.snapshot_id,
                commit_sha=remote.commit_sha,
                baseline=baseline,
            )

        preflight = assess_publication_preflight(local_commit_sha, remote, baseline)
        if not preflight.may_publish:
            raise LocalSyncError("local synchronization lost publication authorization")
        intent = build_publication_intent(workspace, preflight)
        completed = complete_authorized_publication(store, workspace, intent, token)
        disposition = (
            LocalSyncDisposition.INITIALIZED
            if preflight.disposition is PublicationDisposition.SAFE_TO_INITIALIZE
            else LocalSyncDisposition.PUBLISHED
        )
        return LocalSyncResult(
            disposition=disposition,
            target=completed.target,
            repository_id=repository_id,
            branch=completed.branch,
            snapshot_id=completed.snapshot_id,
            commit_sha=completed.commit_sha,
            baseline=completed,
        )
    except LocalSyncError:
        raise
    except (
        StateError,
        SnapshotError,
        WorkspaceError,
        RepositoryVerificationError,
        GitError,
        BaselineAnchorError,
        PublicationPreflightError,
        PublicationIntentError,
        PublicationWorkflowError,
    ) as exc:
        raise LocalSyncError("local synchronization failed closed") from exc
    finally:
        if workspace is not None:
            shutil.rmtree(workspace.root, ignore_errors=True)
        if snapshot is not None:
            shutil.rmtree(snapshot.root, ignore_errors=True)


def _classify_pre_commit_refusal(
    target: str,
    repository_id: int,
    branch: str,
    snapshot: Snapshot,
    remote: BranchHead | BranchAbsence,
    baseline: SynchronizationBaseline | None,
) -> LocalSyncResult | None:
    """Reject states that must never be normalized by creating a local commit."""
    if baseline is None:
        if isinstance(remote, BranchHead):
            return LocalSyncResult(
                disposition=LocalSyncDisposition.BASELINE_REQUIRED,
                target=remote.target,
                repository_id=remote.repository_id,
                branch=remote.branch,
                snapshot_id=snapshot.snapshot_id,
                commit_sha=None,
                baseline=None,
            )
        return None

    if isinstance(remote, BranchAbsence):
        return LocalSyncResult(
            disposition=LocalSyncDisposition.REMOTE_MISSING,
            target=remote.target,
            repository_id=remote.repository_id,
            branch=remote.branch,
            snapshot_id=snapshot.snapshot_id,
            commit_sha=None,
            baseline=baseline,
        )

    if (
        baseline.target.casefold() != target.casefold()
        or baseline.branch != branch
        or remote.repository_id != repository_id
        or remote.target.casefold() != target.casefold()
        or remote.branch != branch
    ):
        raise LocalSyncError("local synchronization evidence does not describe one branch")
    if remote.commit_sha != baseline.commit_sha:
        return LocalSyncResult(
            disposition=LocalSyncDisposition.DIVERGED,
            target=remote.target,
            repository_id=remote.repository_id,
            branch=remote.branch,
            snapshot_id=snapshot.snapshot_id,
            commit_sha=None,
            baseline=baseline,
        )
    return None
