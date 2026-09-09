from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ha_syncapp.git_workspace import GitWorkspace

_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_USER_NAME = "Home Assistant SyncApp"
_USER_EMAIL = "syncapp@localhost"


class GitError(RuntimeError):
    """Raised when a confined local Git operation cannot be trusted."""


@dataclass(frozen=True, slots=True)
class LocalGitRepository:
    tree_path: Path
    default_branch: str
    user_name: str
    user_email: str


def initialize_repository(
    workspace: GitWorkspace, *, default_branch: str = "main"
) -> LocalGitRepository:
    """Initialize machine-owned Git metadata inside an isolated mutable workspace."""
    root, tree = _validate_workspace(workspace)
    _validate_branch(default_branch)
    git_path = tree / ".git"
    if git_path.exists() or os.path.lexists(git_path):
        repository = inspect_repository(workspace)
        if repository.default_branch != default_branch:
            raise GitError("existing repository branch does not match requested branch")
        return repository

    executable = _git_executable()
    _run_git(executable, tree, root, ("init", "--initial-branch", default_branch))
    _run_git(executable, tree, root, ("config", "--local", "user.name", _USER_NAME))
    _run_git(executable, tree, root, ("config", "--local", "user.email", _USER_EMAIL))
    return inspect_repository(workspace)


def inspect_repository(workspace: GitWorkspace) -> LocalGitRepository:
    """Inspect only the local repository metadata of one isolated workspace."""
    root, tree = _validate_workspace(workspace)
    git_path = tree / ".git"
    try:
        metadata = git_path.lstat()
    except OSError as exc:
        raise GitError("workspace Git metadata is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or git_path.is_symlink():
        raise GitError("workspace Git metadata is unsafe")

    executable = _git_executable()
    branch = _run_git(executable, tree, root, ("symbolic-ref", "--short", "HEAD"))
    name = _run_git(executable, tree, root, ("config", "--local", "--get", "user.name"))
    email = _run_git(executable, tree, root, ("config", "--local", "--get", "user.email"))
    _validate_branch(branch)
    if name != _USER_NAME or email != _USER_EMAIL:
        raise GitError("workspace Git identity is not the expected machine identity")
    return LocalGitRepository(
        tree_path=tree,
        default_branch=branch,
        user_name=name,
        user_email=email,
    )


def _validate_workspace(workspace: GitWorkspace) -> tuple[Path, Path]:
    if type(workspace) is not GitWorkspace:
        raise GitError("Git operations require an isolated GitWorkspace")
    root = _real_directory(workspace.root, "workspace root")
    tree = _real_directory(workspace.tree_path, "workspace tree")
    if tree != root / "tree" or root.name.startswith(".git-workspace-") is False:
        raise GitError("workspace layout is not recognized")
    if not root.name.endswith(".tmp"):
        raise GitError("workspace layout is not recognized")
    return root, tree


def _real_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GitError(f"{label} is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise GitError(f"{label} must be a real directory")
    return path.resolve(strict=True)


def _validate_branch(branch: str) -> None:
    if not isinstance(branch, str) or _BRANCH.fullmatch(branch) is None:
        raise GitError("invalid local Git branch")
    if branch in {".", ".."} or branch.endswith((".", ".lock")) or ".." in branch:
        raise GitError("invalid local Git branch")


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise GitError("Git executable is unavailable")
    return executable


def _run_git(executable: str, tree: Path, root: Path, arguments: tuple[str, ...]) -> str:
    environment = {
        "PATH": os.path.dirname(executable),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(  # nosec B603 - executable is resolved to an absolute git path.
            [executable, *arguments],
            cwd=tree,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError("confined Git command could not execute") from exc
    if result.returncode != 0:
        raise GitError("confined Git command failed")
    return result.stdout.strip()
