"""Acquire one trusted Repo B baseline without touching staged Home Assistant bytes."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess  # nosec B404
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote

from ha_syncapp.git_workspace import GitWorkspace, WorkspaceError, verify_workspace_content
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.local_git import GitError, inspect_repository

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TOKEN = re.compile(r"^[!-~]{1,512}$")
_FETCH_REF = "refs/syncapp/trusted-baseline"


class BaselineAnchorError(RuntimeError):
    """Trusted Repo B baseline could not be safely acquired and anchored."""


def anchor_trusted_baseline(
    workspace: GitWorkspace,
    remote: BranchHead,
    token: str,
) -> str:
    """Fetch one exact trusted branch into Git metadata and anchor an unborn local branch."""
    _validate_remote(remote)
    _validate_token(token)
    try:
        repository = inspect_repository(workspace)
        snapshot_id = verify_workspace_content(workspace)
    except (GitError, WorkspaceError) as exc:
        raise BaselineAnchorError("isolated Git workspace could not be re-proven") from exc
    if repository.default_branch != remote.branch:
        raise BaselineAnchorError("trusted baseline branch does not match local branch")
    if snapshot_id != workspace.snapshot_id:
        raise BaselineAnchorError("isolated workspace snapshot identity changed")

    root = workspace.root.resolve(strict=True)
    tree = workspace.tree_path.resolve(strict=True)
    executable = _git_executable()
    if _branch_exists(executable, tree, root, remote.branch):
        raise BaselineAnchorError("local branch is already anchored or committed")

    askpass = _create_askpass(root)
    try:
        repository_url = _repository_url(remote.target)
        refspec = f"+refs/heads/{remote.branch}:{_FETCH_REF}"
        _run_git(
            executable,
            tree,
            root,
            ("fetch", "--no-tags", "--no-recurse-submodules", "--depth=1", repository_url, refspec),
            token=token,
            askpass=askpass,
        )
        fetched = _run_git(
            executable,
            tree,
            root,
            ("rev-parse", "--verify", f"{_FETCH_REF}^{{commit}}"),
        )
        if fetched != remote.commit_sha:
            raise BaselineAnchorError("fetched baseline does not match trusted remote head")
        verify_workspace_content(workspace)
        _run_git(
            executable,
            tree,
            root,
            ("update-ref", f"refs/heads/{remote.branch}", remote.commit_sha),
        )
        anchored = _run_git(executable, tree, root, ("rev-parse", "--verify", "HEAD"))
        if anchored != remote.commit_sha:
            raise BaselineAnchorError("local branch did not anchor to trusted baseline")
        verified = verify_workspace_content(workspace)
        if verified != workspace.snapshot_id:
            raise BaselineAnchorError("isolated workspace changed during baseline acquisition")
        return anchored
    except (GitError, WorkspaceError) as exc:
        raise BaselineAnchorError("trusted baseline acquisition failed") from exc
    finally:
        _delete_fetch_ref(executable, tree, root)
        with suppress(OSError):
            askpass.unlink(missing_ok=True)


def _validate_remote(remote: BranchHead) -> None:
    if type(remote) is not BranchHead:
        raise BaselineAnchorError("trusted branch evidence is invalid")
    if type(remote.repository_id) is not int or remote.repository_id <= 0:
        raise BaselineAnchorError("trusted repository identity is invalid")
    parts = remote.target.split("/") if isinstance(remote.target, str) else []
    if len(parts) != 2 or not all(parts):
        raise BaselineAnchorError("trusted repository target is invalid")
    if not isinstance(remote.branch, str) or not remote.branch:
        raise BaselineAnchorError("trusted repository branch is invalid")
    if _COMMIT_SHA.fullmatch(remote.commit_sha) is None:
        raise BaselineAnchorError("trusted baseline commit identity is invalid")


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise BaselineAnchorError("GitHub authentication is invalid")


def _repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return f"https://github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise BaselineAnchorError("Git executable is unavailable")
    return executable


def _create_askpass(root: Path) -> Path:
    path = root / ".syncapp-askpass"
    script = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *) printf '%s\\n' "$SYNCAPP_GITHUB_TOKEN" ;;
esac
"""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(script)
        metadata = path.lstat()
    except OSError as exc:
        raise BaselineAnchorError("Git authentication helper could not be prepared") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise BaselineAnchorError("Git authentication helper is unsafe")
    return path


def _git_environment(
    executable: str,
    root: Path,
    *,
    token: str | None = None,
    askpass: Path | None = None,
) -> dict[str, str]:
    environment = {
        "PATH": os.path.dirname(executable),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "LC_ALL": "C",
    }
    if token is not None and askpass is not None:
        environment["GIT_ASKPASS"] = str(askpass)
        environment["SYNCAPP_GITHUB_TOKEN"] = token
    return environment


def _command(executable: str, arguments: tuple[str, ...]) -> list[str]:
    return [
        executable,
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        *arguments,
    ]


def _run_git(
    executable: str,
    tree: Path,
    root: Path,
    arguments: tuple[str, ...],
    *,
    token: str | None = None,
    askpass: Path | None = None,
) -> str:
    try:
        result = subprocess.run(  # nosec B603
            _command(executable, arguments),
            cwd=tree,
            env=_git_environment(executable, root, token=token, askpass=askpass),
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


def _branch_exists(executable: str, tree: Path, root: Path, branch: str) -> bool:
    try:
        result = subprocess.run(  # nosec B603
            _command(executable, ("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")),
            cwd=tree,
            env=_git_environment(executable, root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BaselineAnchorError("local branch state could not be inspected") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise BaselineAnchorError("local branch state could not be inspected")


def _delete_fetch_ref(executable: str, tree: Path, root: Path) -> None:
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # nosec B603
            _command(executable, ("update-ref", "-d", _FETCH_REF)),
            cwd=tree,
            env=_git_environment(executable, root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
