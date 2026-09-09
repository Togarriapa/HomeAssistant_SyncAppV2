import os
from pathlib import Path

import pytest
from ha_syncapp.git_workspace import WorkspaceError, prepare_git_workspace, verify_workspace_content
from ha_syncapp.local_git import create_snapshot_commit, initialize_repository
from ha_syncapp.snapshot import capture_snapshot


def _initialized_workspace(tmp_path: Path):
    source = tmp_path / "ha"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    source.mkdir()
    snapshots.mkdir()
    workspaces.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    (source / "nested").mkdir()
    (source / "nested/value").write_bytes(b"stable\x00bytes")
    snapshot = capture_snapshot(source, snapshots)
    workspace = prepare_git_workspace(snapshot.root, workspaces)
    initialize_repository(workspace)
    return workspace


def test_meaningful_verified_snapshot_creates_one_local_commit_and_then_noop(tmp_path: Path) -> None:
    workspace = _initialized_workspace(tmp_path)

    commit = create_snapshot_commit(workspace)

    assert commit is not None
    assert len(commit) in {40, 64}
    assert all(character in "0123456789abcdef" for character in commit)
    assert verify_workspace_content(workspace) == workspace.snapshot_id
    assert create_snapshot_commit(workspace) is None


@pytest.mark.parametrize("tamper", ["modify", "add", "remove", "symlink", "hardlink"])
def test_workspace_tamper_fails_closed_before_commit(tmp_path: Path, tamper: str) -> None:
    workspace = _initialized_workspace(tmp_path)
    target = workspace.tree_path / "configuration.yaml"
    if tamper == "modify":
        target.write_text("changed")
    elif tamper == "add":
        (workspace.tree_path / "extra.yaml").write_text("extra")
    elif tamper == "remove":
        target.unlink()
    elif tamper == "symlink":
        target.unlink()
        target.symlink_to(workspace.tree_path / "nested/value")
    else:
        os.link(target, workspace.tree_path / "hardlink.yaml")

    with pytest.raises(WorkspaceError):
        create_snapshot_commit(workspace)


def test_nested_git_metadata_in_source_is_rejected_before_workspace_creation(tmp_path: Path) -> None:
    source = tmp_path / "ha"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    (source / "custom/.git").mkdir(parents=True)
    snapshots.mkdir()
    workspaces.mkdir()
    (source / "custom/.git/config").write_text("not repository metadata for SyncApp")
    snapshot = capture_snapshot(source, snapshots)

    with pytest.raises(WorkspaceError):
        prepare_git_workspace(snapshot.root, workspaces)


def test_post_commit_hook_is_not_executed(tmp_path: Path) -> None:
    workspace = _initialized_workspace(tmp_path)
    sentinel = tmp_path / "hook-ran"
    hook = workspace.tree_path / ".git/hooks/post-commit"
    hook.write_text(f"#!/bin/sh\necho ran > {sentinel}\n")
    os.chmod(hook, 0o700)

    assert create_snapshot_commit(workspace) is not None
    assert not sentinel.exists()


def test_signing_configuration_cannot_force_machine_commit_prompt(tmp_path: Path) -> None:
    workspace = _initialized_workspace(tmp_path)
    config = workspace.tree_path / ".git/config"
    with config.open("a") as handle:
        handle.write("\n[commit]\n\tgpgSign = true\n")

    assert create_snapshot_commit(workspace) is not None
