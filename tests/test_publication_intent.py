import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp.git_workspace import GitWorkspace, prepare_git_workspace
from ha_syncapp.github_repo import BranchAbsence, BranchHead
from ha_syncapp.local_git import GitError, create_snapshot_commit, initialize_repository
from ha_syncapp.publication_intent import (
    PublicationIntentError,
    build_publication_intent,
)
from ha_syncapp.publication_preflight import (
    PublicationDisposition,
    PublicationPreflight,
    assess_publication_preflight,
)
from ha_syncapp.snapshot import capture_snapshot
from ha_syncapp.state import SynchronizationBaseline


def _workspace(tmp_path: Path) -> GitWorkspace:
    source = tmp_path / "ha"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    source.mkdir()
    snapshots.mkdir()
    workspaces.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    snapshot = capture_snapshot(source, snapshots)
    workspace = prepare_git_workspace(snapshot.root, workspaces)
    initialize_repository(workspace)
    return workspace


def _git(workspace: GitWorkspace, *arguments: str) -> str:
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(workspace.root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    result = subprocess.run(
        ["git", *arguments],
        cwd=workspace.tree_path,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _baseline(commit_sha: str) -> SynchronizationBaseline:
    return SynchronizationBaseline(
        target="Owner/Home",
        branch="main",
        snapshot_id="d" * 64,
        commit_sha=commit_sha,
        synchronized_at=datetime(2026, 9, 9, tzinfo=UTC),
    )


def test_normal_publication_intent_binds_exact_remote_and_proves_ancestry(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _git(workspace, "commit", "--allow-empty", "--no-gpg-sign", "--no-verify", "-m", "baseline")
    baseline_sha = _git(workspace, "rev-parse", "HEAD")
    local_sha = create_snapshot_commit(workspace)
    assert local_sha is not None
    preflight = assess_publication_preflight(
        local_sha,
        BranchHead("Owner/Home", 42, "main", baseline_sha),
        _baseline(baseline_sha),
    )

    intent = build_publication_intent(workspace, preflight)

    assert intent.target == "Owner/Home"
    assert intent.repository_id == 42
    assert intent.branch == "main"
    assert intent.local_commit_sha == local_sha
    assert intent.expected_remote_commit_sha == baseline_sha
    assert intent.expect_remote_absent is False


def test_first_publication_intent_binds_exact_remote_absence(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    local_sha = create_snapshot_commit(workspace)
    assert local_sha is not None
    preflight = assess_publication_preflight(
        local_sha,
        BranchAbsence("Owner/Home", 42, "main"),
        None,
    )

    intent = build_publication_intent(workspace, preflight)

    assert intent.local_commit_sha == local_sha
    assert intent.expected_remote_commit_sha is None
    assert intent.expect_remote_absent is True


@pytest.mark.parametrize(
    "disposition",
    [
        PublicationDisposition.NO_CHANGE,
        PublicationDisposition.ALREADY_PUBLISHED,
        PublicationDisposition.DIVERGED,
        PublicationDisposition.REMOTE_MISSING,
        PublicationDisposition.BASELINE_REQUIRED,
    ],
)
def test_non_authorizing_preflight_states_fail_closed(
    tmp_path: Path, disposition: PublicationDisposition
) -> None:
    workspace = _workspace(tmp_path)
    local_sha = create_snapshot_commit(workspace)
    assert local_sha is not None
    preflight = PublicationPreflight(
        disposition=disposition,
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=local_sha,
        remote_commit_sha=local_sha,
        baseline_commit_sha=local_sha,
    )

    with pytest.raises(PublicationIntentError, match="does not authorize"):
        build_publication_intent(workspace, preflight)


def test_forged_normal_publication_with_mismatched_remote_and_baseline_is_rejected(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    local_sha = create_snapshot_commit(workspace)
    assert local_sha is not None
    preflight = PublicationPreflight(
        disposition=PublicationDisposition.SAFE_TO_PUBLISH,
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=local_sha,
        remote_commit_sha="a" * 40,
        baseline_commit_sha="b" * 40,
    )

    with pytest.raises(PublicationIntentError, match="does not match baseline"):
        build_publication_intent(workspace, preflight)


def test_forged_initialization_with_remote_sha_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    local_sha = create_snapshot_commit(workspace)
    assert local_sha is not None
    preflight = PublicationPreflight(
        disposition=PublicationDisposition.SAFE_TO_INITIALIZE,
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=local_sha,
        remote_commit_sha="a" * 40,
        baseline_commit_sha=None,
    )

    with pytest.raises(PublicationIntentError, match="absent remote"):
        build_publication_intent(workspace, preflight)


def test_normal_publication_rejects_unrelated_local_history(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _git(workspace, "commit", "--allow-empty", "--no-gpg-sign", "--no-verify", "-m", "baseline")
    baseline_sha = _git(workspace, "rev-parse", "HEAD")
    local_sha = create_snapshot_commit(workspace)
    assert local_sha is not None
    tree_sha = _git(workspace, "write-tree")
    unrelated_sha = _git(workspace, "commit-tree", tree_sha, "-m", "unrelated")
    preflight = PublicationPreflight(
        disposition=PublicationDisposition.SAFE_TO_PUBLISH,
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=unrelated_sha,
        remote_commit_sha=baseline_sha,
        baseline_commit_sha=baseline_sha,
    )

    with pytest.raises(GitError, match="does not descend"):
        build_publication_intent(workspace, preflight)
