"""Guarded publication of one consistent Recorder snapshot to Repo B database."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ha_syncapp.baseline_anchor import BaselineAnchorError, anchor_trusted_baseline
from ha_syncapp.database_snapshot import (
    DatabaseSnapshot,
    DatabaseSnapshotError,
    capture_sqlite_snapshot,
)
from ha_syncapp.git_workspace import GitWorkspace, WorkspaceError, prepare_git_workspace
from ha_syncapp.github_repo import (
    BranchAbsence,
    BranchHead,
    RepositoryVerificationError,
    fetch_optional_trusted_branch_head,
)
from ha_syncapp.local_git import GitError, create_snapshot_commit, initialize_repository
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
from ha_syncapp.snapshot import Snapshot, SnapshotError, capture_snapshot
from ha_syncapp.state import StateError, StateStore, SynchronizationBaseline

_DATABASE_BRANCH = "database"


class DatabaseSyncError(RuntimeError):
    """A Recorder database publication cycle could not complete safely."""


class DatabaseSyncDisposition(StrEnum):
    """Externally useful result of one guarded database publication cycle."""

    INITIALIZED = "initialized"
    PUBLISHED = "published"
    NO_CHANGE = "no_change"
    BASELINE_REQUIRED = "baseline_required"
    DIVERGED = "diverged"
    REMOTE_MISSING = "remote_missing"


@dataclass(frozen=True, slots=True)
class DatabaseSyncResult:
    disposition: DatabaseSyncDisposition
    target: str
    repository_id: int
    branch: str
    database_sha256: str
    database_size: int
    snapshot_id: str
    commit_sha: str | None
    baseline: SynchronizationBaseline | None


def synchronize_database_snapshot(
    store: StateStore,
    source_database: Path,
    database_staging_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
) -> DatabaseSyncResult:
    """Capture, classify and when authorized publish one Recorder database backup."""
    if type(store) is not StateStore:
        raise DatabaseSyncError("database synchronization state store is invalid")

    database_snapshot: DatabaseSnapshot | None = None
    snapshot: Snapshot | None = None
    workspace: GitWorkspace | None = None
    try:
        repository_id = store.repository_id(target)
        if repository_id is None:
            raise DatabaseSyncError("database synchronization repository is not pinned")

        database_snapshot = capture_sqlite_snapshot(source_database, database_staging_root)
        snapshot = capture_snapshot(database_snapshot.root, snapshot_staging_root)
        workspace = prepare_git_workspace(snapshot.root, workspace_root)
        initialize_repository(workspace, default_branch=_DATABASE_BRANCH)

        remote = fetch_optional_trusted_branch_head(
            target,
            token,
            expected_id=repository_id,
            branch=_DATABASE_BRANCH,
        )
        baseline = store.synchronization_baseline(target, _DATABASE_BRANCH)
        refusal = _classify_refusal(
            target,
            repository_id,
            database_snapshot,
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
                raise DatabaseSyncError("initial database synchronization produced no commit")
            return _result(
                DatabaseSyncDisposition.NO_CHANGE,
                remote.target,
                repository_id,
                database_snapshot,
                snapshot,
                remote.commit_sha,
                baseline,
            )

        preflight = assess_publication_preflight(local_commit_sha, remote, baseline)
        if not preflight.may_publish:
            raise DatabaseSyncError("database synchronization lost publication authorization")
        intent = build_publication_intent(workspace, preflight)
        completed = complete_authorized_publication(store, workspace, intent, token)
        disposition = (
            DatabaseSyncDisposition.INITIALIZED
            if preflight.disposition is PublicationDisposition.SAFE_TO_INITIALIZE
            else DatabaseSyncDisposition.PUBLISHED
        )
        return _result(
            disposition,
            completed.target,
            repository_id,
            database_snapshot,
            snapshot,
            completed.commit_sha,
            completed,
        )
    except DatabaseSyncError:
        raise
    except (
        StateError,
        DatabaseSnapshotError,
        SnapshotError,
        WorkspaceError,
        RepositoryVerificationError,
        GitError,
        BaselineAnchorError,
        PublicationPreflightError,
        PublicationIntentError,
        PublicationWorkflowError,
    ) as exc:
        raise DatabaseSyncError("database synchronization failed closed") from exc
    finally:
        if workspace is not None:
            shutil.rmtree(workspace.root, ignore_errors=True)
        if snapshot is not None:
            shutil.rmtree(snapshot.root, ignore_errors=True)
        if database_snapshot is not None:
            shutil.rmtree(database_snapshot.root, ignore_errors=True)


def _classify_refusal(
    target: str,
    repository_id: int,
    database_snapshot: DatabaseSnapshot,
    snapshot: Snapshot,
    remote: BranchHead | BranchAbsence,
    baseline: SynchronizationBaseline | None,
) -> DatabaseSyncResult | None:
    if baseline is None:
        if isinstance(remote, BranchHead):
            return _result(
                DatabaseSyncDisposition.BASELINE_REQUIRED,
                remote.target,
                repository_id,
                database_snapshot,
                snapshot,
                None,
                None,
            )
        return None

    if isinstance(remote, BranchAbsence):
        return _result(
            DatabaseSyncDisposition.REMOTE_MISSING,
            remote.target,
            repository_id,
            database_snapshot,
            snapshot,
            None,
            baseline,
        )

    if (
        baseline.target.casefold() != target.casefold()
        or baseline.branch != _DATABASE_BRANCH
        or remote.repository_id != repository_id
        or remote.target.casefold() != target.casefold()
        or remote.branch != _DATABASE_BRANCH
    ):
        raise DatabaseSyncError("database synchronization evidence describes another branch")
    if remote.commit_sha != baseline.commit_sha:
        return _result(
            DatabaseSyncDisposition.DIVERGED,
            remote.target,
            repository_id,
            database_snapshot,
            snapshot,
            None,
            baseline,
        )
    return None


def _result(
    disposition: DatabaseSyncDisposition,
    target: str,
    repository_id: int,
    database_snapshot: DatabaseSnapshot,
    snapshot: Snapshot,
    commit_sha: str | None,
    baseline: SynchronizationBaseline | None,
) -> DatabaseSyncResult:
    return DatabaseSyncResult(
        disposition=disposition,
        target=target,
        repository_id=repository_id,
        branch=_DATABASE_BRANCH,
        database_sha256=database_snapshot.sha256,
        database_size=database_snapshot.size,
        snapshot_id=snapshot.snapshot_id,
        commit_sha=commit_sha,
        baseline=baseline,
    )
