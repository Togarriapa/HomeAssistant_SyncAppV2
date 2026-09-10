"""Guarded publication of verified log artifacts to Repo B logs."""

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
from ha_syncapp.log_artifact import LogArtifact, LogArtifactError, verify_log_artifact
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

_LOGS_BRANCH = "logs"


class LogSyncError(RuntimeError):
    """A log artifact publication cycle could not complete safely."""


class LogSyncDisposition(StrEnum):
    """Externally useful result of one guarded logs publication cycle."""

    INITIALIZED = "initialized"
    PUBLISHED = "published"
    NO_CHANGE = "no_change"
    BASELINE_REQUIRED = "baseline_required"
    DIVERGED = "diverged"
    REMOTE_MISSING = "remote_missing"


@dataclass(frozen=True, slots=True)
class LogSyncResult:
    """Result evidence for one logs publication cycle."""

    disposition: LogSyncDisposition
    target: str
    repository_id: int
    branch: str
    artifact_id: str
    snapshot_id: str
    commit_sha: str | None
    baseline: SynchronizationBaseline | None


def synchronize_log_artifact(
    store: StateStore,
    artifact: LogArtifact,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
) -> LogSyncResult:
    """Classify and, when authorized, publish one already-staged log artifact."""
    if type(store) is not StateStore:
        raise LogSyncError("logs synchronization state store is invalid")

    snapshot: Snapshot | None = None
    workspace: GitWorkspace | None = None
    try:
        repository_id = store.repository_id(target)
        if repository_id is None:
            raise LogSyncError("logs synchronization repository is not pinned")

        verify_log_artifact(artifact)
        snapshot = capture_snapshot(artifact.root, snapshot_staging_root)
        workspace = prepare_git_workspace(snapshot.root, workspace_root)
        initialize_repository(workspace, default_branch=_LOGS_BRANCH)

        remote = fetch_optional_trusted_branch_head(
            target,
            token,
            expected_id=repository_id,
            branch=_LOGS_BRANCH,
        )
        baseline = store.synchronization_baseline(target, _LOGS_BRANCH)
        refusal = _classify_refusal(
            target,
            repository_id,
            artifact,
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
                raise LogSyncError("initial logs synchronization produced no commit")
            return _result(
                LogSyncDisposition.NO_CHANGE,
                remote.target,
                repository_id,
                artifact,
                snapshot,
                remote.commit_sha,
                baseline,
            )

        preflight = assess_publication_preflight(local_commit_sha, remote, baseline)
        if not preflight.may_publish:
            raise LogSyncError("logs synchronization lost publication authorization")
        intent = build_publication_intent(workspace, preflight)
        completed = complete_authorized_publication(store, workspace, intent, token)
        disposition = (
            LogSyncDisposition.INITIALIZED
            if preflight.disposition is PublicationDisposition.SAFE_TO_INITIALIZE
            else LogSyncDisposition.PUBLISHED
        )
        return _result(
            disposition,
            completed.target,
            repository_id,
            artifact,
            snapshot,
            completed.commit_sha,
            completed,
        )
    except LogSyncError:
        raise
    except (
        StateError,
        LogArtifactError,
        SnapshotError,
        WorkspaceError,
        RepositoryVerificationError,
        GitError,
        BaselineAnchorError,
        PublicationPreflightError,
        PublicationIntentError,
        PublicationWorkflowError,
    ) as exc:
        raise LogSyncError("logs synchronization failed closed") from exc
    finally:
        if workspace is not None:
            shutil.rmtree(workspace.root, ignore_errors=True)
        if snapshot is not None:
            shutil.rmtree(snapshot.root, ignore_errors=True)


def _classify_refusal(
    target: str,
    repository_id: int,
    artifact: LogArtifact,
    snapshot: Snapshot,
    remote: BranchHead | BranchAbsence,
    baseline: SynchronizationBaseline | None,
) -> LogSyncResult | None:
    if baseline is None:
        if isinstance(remote, BranchHead):
            return _result(
                LogSyncDisposition.BASELINE_REQUIRED,
                remote.target,
                repository_id,
                artifact,
                snapshot,
                None,
                None,
            )
        return None

    if isinstance(remote, BranchAbsence):
        return _result(
            LogSyncDisposition.REMOTE_MISSING,
            remote.target,
            repository_id,
            artifact,
            snapshot,
            None,
            baseline,
        )

    if (
        baseline.target.casefold() != target.casefold()
        or baseline.branch != _LOGS_BRANCH
        or remote.repository_id != repository_id
        or remote.target.casefold() != target.casefold()
        or remote.branch != _LOGS_BRANCH
    ):
        raise LogSyncError("logs synchronization evidence describes another branch")
    if remote.commit_sha != baseline.commit_sha:
        return _result(
            LogSyncDisposition.DIVERGED,
            remote.target,
            repository_id,
            artifact,
            snapshot,
            None,
            baseline,
        )
    return None


def _result(
    disposition: LogSyncDisposition,
    target: str,
    repository_id: int,
    artifact: LogArtifact,
    snapshot: Snapshot,
    commit_sha: str | None,
    baseline: SynchronizationBaseline | None,
) -> LogSyncResult:
    return LogSyncResult(
        disposition=disposition,
        target=target,
        repository_id=repository_id,
        branch=_LOGS_BRANCH,
        artifact_id=artifact.artifact_id,
        snapshot_id=snapshot.snapshot_id,
        commit_sha=commit_sha,
        baseline=baseline,
    )
