"""Non-force Repo B publication transport from an isolated Git workspace."""

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
from ha_syncapp.github_repo import BranchAbsence, BranchHead
from ha_syncapp.local_git import GitError, inspect_repository
from ha_syncapp.publication_intent import PublicationIntent

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TOKEN = re.compile(r"^[!-~]{1,512}$")


class PublicationTransportError(RuntimeError):
    """An authorized Repo B publication could not be safely transported."""


def push_publication_intent(
    workspace: GitWorkspace,
    intent: PublicationIntent,
    current_remote: BranchHead | BranchAbsence,
    token: str,
) -> str:
    """Push exactly one authorized commit without force after fresh remote proof."""
    _validate_intent(intent)
    _validate_current_remote(intent, current_remote)
    _validate_token(token)
    try:
        repository = inspect_repository(workspace)
        snapshot_id = verify_workspace_content(workspace)
    except (GitError, WorkspaceError) as exc:
        raise PublicationTransportError("isolated publication workspace could not be re-proven") from exc
    if repository.default_branch != intent.branch:
        raise PublicationTransportError("publication branch does not match isolated workspace")
    if snapshot_id != workspace.snapshot_id:
        raise PublicationTransportError("isolated publication snapshot identity changed")

    root = workspace.root.resolve(strict=True)
    tree = workspace.tree_path.resolve(strict=True)
    executable = _git_executable()
    try:
        local_head = _run_git(executable, tree, root, ("rev-parse", "--verify", "HEAD"))
    except GitError as exc:
        raise PublicationTransportError("local publication commit could not be re-proven") from exc
    if local_head != intent.local_commit_sha:
        raise PublicationTransportError("local publication commit changed after authorization")

    askpass = _create_askpass(root)
    try:
        repository_url = _repository_url(intent.target)
        refspec = f"{intent.local_commit_sha}:refs/heads/{intent.branch}"
        _run_git(
            executable,
            tree,
            root,
            (
                "push",
                "--porcelain",
                "--no-verify",
                repository_url,
                refspec,
            ),
            token=token,
            askpass=askpass,
        )
    except GitError as exc:
        _verify_after_transport(workspace)
        raise PublicationTransportError("Repo B publication transport was rejected") from exc
    finally:
        with suppress(OSError):
            askpass.unlink(missing_ok=True)

    _verify_after_transport(workspace)
    return intent.local_commit_sha


def _validate_intent(intent: PublicationIntent) -> None:
    if type(intent) is not PublicationIntent:
        raise PublicationTransportError("publication intent evidence is invalid")
    if type(intent.repository_id) is not int or intent.repository_id <= 0:
        raise PublicationTransportError("publication repository identity is invalid")
    parts = intent.target.split("/") if isinstance(intent.target, str) else []
    if len(parts) != 2 or not all(parts):
        raise PublicationTransportError("publication repository target is invalid")
    if not isinstance(intent.branch, str) or not intent.branch:
        raise PublicationTransportError("publication branch identity is invalid")
    if _COMMIT_SHA.fullmatch(intent.local_commit_sha) is None:
        raise PublicationTransportError("publication local commit identity is invalid")
    if intent.expect_remote_absent:
        if intent.expected_remote_commit_sha is not None:
            raise PublicationTransportError("publication initialization intent is inconsistent")
    elif (
        intent.expected_remote_commit_sha is None
        or _COMMIT_SHA.fullmatch(intent.expected_remote_commit_sha) is None
    ):
        raise PublicationTransportError("publication remote expectation is invalid")


def _validate_current_remote(
    intent: PublicationIntent,
    current_remote: BranchHead | BranchAbsence,
) -> None:
    if intent.expect_remote_absent:
        if type(current_remote) is not BranchAbsence:
            raise PublicationTransportError("publication target branch is no longer absent")
        _validate_remote_identity(intent, current_remote)
        return
    if type(current_remote) is not BranchHead:
        raise PublicationTransportError("publication target branch is no longer at expected baseline")
    _validate_remote_identity(intent, current_remote)
    if current_remote.commit_sha != intent.expected_remote_commit_sha:
        raise PublicationTransportError("publication target branch changed after authorization")


def _validate_remote_identity(
    intent: PublicationIntent,
    current_remote: BranchHead | BranchAbsence,
) -> None:
    if type(current_remote.repository_id) is not int or current_remote.repository_id <= 0:
        raise PublicationTransportError("trusted publication repository identity is invalid")
    if intent.target.casefold() != current_remote.target.casefold():
        raise PublicationTransportError("trusted publication repository target changed")
    if intent.repository_id != current_remote.repository_id:
        raise PublicationTransportError("trusted publication repository identity changed")
    if intent.branch != current_remote.branch:
        raise PublicationTransportError("trusted publication branch changed")


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise PublicationTransportError("GitHub authentication is invalid")


def _repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return f"https://github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise PublicationTransportError("Git executable is unavailable")
    return executable


def _create_askpass(root: Path) -> Path:
    path = root / ".syncapp-push-askpass"
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
        raise PublicationTransportError("Git authentication helper could not be prepared") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise PublicationTransportError("Git authentication helper is unsafe")
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


def _verify_after_transport(workspace: GitWorkspace) -> None:
    try:
        snapshot_id = verify_workspace_content(workspace)
    except WorkspaceError as exc:
        raise PublicationTransportError("isolated publication workspace changed during transport") from exc
    if snapshot_id != workspace.snapshot_id:
        raise PublicationTransportError("isolated publication snapshot identity changed during transport")
