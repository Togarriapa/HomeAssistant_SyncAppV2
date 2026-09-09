import os
from pathlib import Path

import pytest
from ha_syncapp import git_workspace as workspace_module
from ha_syncapp.git_workspace import WorkspaceError, prepare_git_workspace
from ha_syncapp.snapshot import SnapshotError, capture_snapshot, verify_snapshot


def _snapshot(tmp_path: Path):
    source = tmp_path / "ha"
    staging = tmp_path / "snapshots"
    source.mkdir()
    staging.mkdir()
    (source / "nested").mkdir()
    (source / "configuration.yaml").write_bytes(b"homeassistant:\n")
    executable = source / "nested/tool"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    os.chmod(executable, 0o700)
    return capture_snapshot(source, staging)


def test_verified_snapshot_is_materialized_byte_for_byte_into_unique_workspaces(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    first = prepare_git_workspace(snapshot.root, workspace_root)
    second = prepare_git_workspace(snapshot.root, workspace_root)

    assert first.root != second.root
    assert first.snapshot_id == snapshot.snapshot_id == second.snapshot_id
    assert (first.tree_path / "configuration.yaml").read_bytes() == b"homeassistant:\n"
    assert (first.tree_path / "nested/tool").read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert (first.tree_path / "nested/tool").stat().st_mode & 0o777 == 0o700
    assert not (first.tree_path / ".git").exists()
    assert not (second.tree_path / ".git").exists()


def test_workspace_mutation_does_not_change_or_invalidate_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    workspace = prepare_git_workspace(snapshot.root, workspace_root)

    (workspace.tree_path / "configuration.yaml").write_text("changed")
    (workspace.tree_path / "git-created-file").write_text("mutable")

    assert (snapshot.tree_path / "configuration.yaml").read_bytes() == b"homeassistant:\n"
    assert verify_snapshot(snapshot.root) == snapshot


def test_snapshot_and_workspace_roots_must_not_overlap(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)

    with pytest.raises(WorkspaceError):
        prepare_git_workspace(snapshot.root, snapshot.root)

    inside = snapshot.root / "workspace"
    inside.mkdir()
    with pytest.raises(WorkspaceError):
        prepare_git_workspace(snapshot.root, inside)


def test_symlinked_workspace_root_is_rejected(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    real = tmp_path / "real-workspaces"
    real.mkdir()
    linked = tmp_path / "linked-workspaces"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(WorkspaceError):
        prepare_git_workspace(snapshot.root, linked)
    assert list(real.iterdir()) == []


def test_snapshot_mutation_during_materialization_fails_closed_and_removes_partial_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    original_copy = workspace_module._copy_snapshot_file
    changed = False

    def mutate_after_copy(*args: object, **kwargs: object) -> None:
        nonlocal changed
        original_copy(*args, **kwargs)
        if not changed:
            (snapshot.tree_path / "configuration.yaml").write_text("tampered")
            changed = True

    monkeypatch.setattr(workspace_module, "_copy_snapshot_file", mutate_after_copy)

    with pytest.raises(SnapshotError):
        prepare_git_workspace(snapshot.root, workspace_root)
    assert list(workspace_root.iterdir()) == []
