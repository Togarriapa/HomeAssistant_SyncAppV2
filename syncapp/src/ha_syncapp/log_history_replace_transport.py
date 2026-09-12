from __future__ import annotations

import math
import os
import re
import shutil
import stat
import subprocess  # nosec B404
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from ha_syncapp.log_history_replacement import LogHistoryReplacementAuthorization

LOG_HISTORY_BRANCH = "logs"
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPO_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_MAX_TIMEOUT_SECONDS = 300.0


class LogHistoryReplacementTransportError(RuntimeError):
    """Raised when the final logs history replacement cannot be completed safely."""


class CommandRunner(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def _run_git(
    command: tuple[str, ...],
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def replace_logs_history(
    *,
    authorization: LogHistoryReplacementAuthorization,
    repository: Path,
    replacement_head_sha: str,
    timeout: float = 30.0,
    runner: CommandRunner = _run_git,
) -> bool:
    """Atomically replace only `logs` when its remote head still matches the lease."""

    if type(authorization) is not LogHistoryReplacementAuthorization:
        raise LogHistoryReplacementTransportError("logs replacement authorization is invalid")
    if authorization.branch != LOG_HISTORY_BRANCH:
        raise LogHistoryReplacementTransportError("history replacement is restricted to logs")
    _validate_repository_identity(authorization)
    if not authorization.requires_replacement:
        return False
    repository = _validate_repository_path(repository)
    if (
        not isinstance(authorization.expected_head_sha, str)
        or _COMMIT_SHA.fullmatch(authorization.expected_head_sha) is None
    ):
        raise LogHistoryReplacementTransportError("logs replacement authorization is invalid")
    if (
        not isinstance(replacement_head_sha, str)
        or _COMMIT_SHA.fullmatch(replacement_head_sha) is None
        or len(replacement_head_sha) != len(authorization.expected_head_sha)
        or replacement_head_sha == authorization.expected_head_sha
    ):
        raise LogHistoryReplacementTransportError("logs replacement head is invalid")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or not 0 < timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise LogHistoryReplacementTransportError("logs replacement timeout is invalid")

    executable = _git_executable()
    ref = f"refs/heads/{LOG_HISTORY_BRANCH}"
    command = (
        executable,
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        "push",
        "--porcelain",
        "--no-verify",
        _repository_url(authorization.target),
        f"{replacement_head_sha}:{ref}",
        f"--force-with-lease={ref}:{authorization.expected_head_sha}",
    )
    try:
        result = runner(command, cwd=repository, timeout=float(timeout))
    except (OSError, subprocess.SubprocessError, TimeoutError):
        raise LogHistoryReplacementTransportError(
            "logs history replacement transport failed"
        ) from None

    if not isinstance(result, subprocess.CompletedProcess):
        raise LogHistoryReplacementTransportError("logs history replacement transport failed")
    if result.returncode != 0:
        raise LogHistoryReplacementTransportError("logs history replacement was rejected")
    return True


def _validate_repository_identity(
    authorization: LogHistoryReplacementAuthorization,
) -> None:
    parts = authorization.target.split("/") if isinstance(authorization.target, str) else []
    if (
        len(parts) != 2
        or _REPO_OWNER.fullmatch(parts[0]) is None
        or _REPO_NAME.fullmatch(parts[1]) is None
        or parts[1] in {".", ".."}
        or type(authorization.repository_id) is not int
        or authorization.repository_id <= 0
    ):
        raise LogHistoryReplacementTransportError("logs replacement repository identity is invalid")


def _validate_repository_path(repository: Path) -> Path:
    if not isinstance(repository, Path) or not repository.is_absolute():
        raise LogHistoryReplacementTransportError("logs replacement repository path is invalid")
    try:
        metadata = repository.lstat()
        resolved = repository.resolve(strict=True)
    except OSError:
        raise LogHistoryReplacementTransportError(
            "logs replacement repository path is invalid"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode) or repository.is_symlink():
        raise LogHistoryReplacementTransportError("logs replacement repository path is invalid")
    return resolved


def _repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return f"https://github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise LogHistoryReplacementTransportError("Git executable is unavailable")
    return executable
