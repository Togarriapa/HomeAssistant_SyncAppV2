import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from ha_syncapp.git_workspace import GitWorkspace, prepare_git_workspace
from ha_syncapp.local_git import GitError, initialize_repository, inspect_repository
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
    return prepare_git_workspace(snapshot.root, workspaces)


def test_repository_is_initialized_only_inside_mutable_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    repository = initialize_repository(workspace, default_branch="main")

    assert repository.tree_path == workspace.tree_path
    assert repository.default_branch == "main"
    assert repository.user_name == "Home Assistant SyncApp"
    assert repository.user_email == "syncapp@localhost"
    assert (workspace.tree_path / ".git").is_dir()
    assert not (workspace.root / ".git").exists()
    assert (workspace.tree_path / "configuration.yaml").read_text() == "homeassistant:\n"
    assert inspect_repository(workspace) == repository


def test_repository_identity_is_local_and_global_git_config_is_not_changed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    home = tmp_path / "caller-home"
    home.mkdir()
    global_config = home / ".gitconfig"
    global_config.write_text("[user]\n\tname = Caller\n\temail = caller@example.invalid\n")
    before = global_config.read_bytes()

    repository = initialize_repository(workspace, default_branch="sync-main")

    assert repository.default_branch == "sync-main"
    assert repository.user_name == "Home Assistant SyncApp"
    assert repository.user_email == "syncapp@localhost"
    assert global_config.read_bytes() == before


def test_initialization_is_idempotent_for_same_workspace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    first = initialize_repository(workspace, default_branch="main")
    second = initialize_repository(workspace, default_branch="main")

    assert second == first


def test_non_workspace_missing_and_symlinked_trees_fail_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(GitError):
        initialize_repository(SimpleNamespace(root=workspace.root, tree_path=workspace.tree_path))  # type: ignore[arg-type]

    missing = GitWorkspace(workspace.snapshot_id, workspace.root, workspace.root / "missing")
    with pytest.raises(GitError):
        initialize_repository(missing)

    real_tree = workspace.tree_path
    linked = workspace.root / "linked-tree"
    linked.symlink_to(real_tree, target_is_directory=True)
    forged = GitWorkspace(workspace.snapshot_id, workspace.root, linked)
    with pytest.raises(GitError):
        initialize_repository(forged)


def test_invalid_branch_is_rejected_before_git_runs(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    for branch in ("", "bad branch", "../escape", "x.lock", "refs/heads/x"):
        with pytest.raises(GitError):
            initialize_repository(workspace, default_branch=branch)
    assert not (workspace.tree_path / ".git").exists()


def test_git_failure_does_not_disclose_subprocess_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"], returncode=1, stdout="secret-sentinel-out", stderr="secret-sentinel-err"
        )

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(GitError) as error:
        initialize_repository(workspace)
    assert "secret-sentinel" not in str(error.value)
