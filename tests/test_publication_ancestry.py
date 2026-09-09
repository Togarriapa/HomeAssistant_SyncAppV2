import os
import subprocess
from pathlib import Path

import pytest
from ha_syncapp.git_workspace import GitWorkspace, prepare_git_workspace
from ha_syncapp.local_git import (
    GitError,
    create_snapshot_commit,
    initialize_repository,
    verify_fast_forward_ancestry,
)
from ha_syncapp.snapshot import capture_snapshot


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


def test_equal_commit_is_safe_fast_forward_boundary(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    baseline = create_snapshot_commit(workspace)
    assert baseline is not None

    verify_fast_forward_ancestry(
        workspace,
        baseline_commit_sha=baseline,
        local_commit_sha=baseline,
    )


def test_descendant_commit_is_accepted_without_changing_workspace_bytes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    baseline = create_snapshot_commit(workspace)
    assert baseline is not None
    before = (workspace.tree_path / "configuration.yaml").read_bytes()
    _git(workspace, "commit", "--allow-empty", "--no-gpg-sign", "--no-verify", "-m", "child")
    local_commit = _git(workspace, "rev-parse", "HEAD")

    verify_fast_forward_ancestry(
        workspace,
        baseline_commit_sha=baseline,
        local_commit_sha=local_commit,
    )

    assert (workspace.tree_path / "configuration.yaml").read_bytes() == before


def test_unrelated_existing_commit_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    baseline = create_snapshot_commit(workspace)
    assert baseline is not None
    tree_sha = _git(workspace, "write-tree")
    unrelated = _git(workspace, "commit-tree", tree_sha, "-m", "unrelated")

    with pytest.raises(GitError, match="does not descend"):
        verify_fast_forward_ancestry(
            workspace,
            baseline_commit_sha=baseline,
            local_commit_sha=unrelated,
        )


def test_missing_commit_object_fails_closed_without_git_output(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    baseline = create_snapshot_commit(workspace)
    assert baseline is not None
    missing = "f" * 40

    with pytest.raises(GitError) as error:
        verify_fast_forward_ancestry(
            workspace,
            baseline_commit_sha=baseline,
            local_commit_sha=missing,
        )

    assert missing not in str(error.value)


def test_malformed_commit_identity_is_rejected_before_git_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("git must not run for malformed commit identity")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(GitError, match="baseline commit identity is invalid"):
        verify_fast_forward_ancestry(
            workspace,
            baseline_commit_sha="not-a-sha",
            local_commit_sha="a" * 40,
        )
