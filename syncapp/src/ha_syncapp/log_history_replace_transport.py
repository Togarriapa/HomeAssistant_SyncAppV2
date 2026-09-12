from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Protocol

from ha_syncapp.log_history_replacement import LogHistoryReplacementAuthorization

LOG_HISTORY_BRANCH = "logs"
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


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
    return subprocess.run(  # noqa: S603
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
    if not authorization.requires_replacement:
        return False
    if not isinstance(repository, Path) or not repository.is_absolute():
        raise LogHistoryReplacementTransportError("logs replacement repository path is invalid")
    if (
        not isinstance(replacement_head_sha, str)
        or _COMMIT_SHA.fullmatch(replacement_head_sha) is None
        or replacement_head_sha == authorization.expected_head_sha
    ):
        raise LogHistoryReplacementTransportError("logs replacement head is invalid")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise LogHistoryReplacementTransportError("logs replacement timeout is invalid")

    ref = f"refs/heads/{LOG_HISTORY_BRANCH}"
    command = (
        "git",
        "push",
        "origin",
        f"{replacement_head_sha}:{ref}",
        f"--force-with-lease={ref}:{authorization.expected_head_sha}",
    )
    try:
        result = runner(command, cwd=repository, timeout=float(timeout))
    except (OSError, subprocess.SubprocessError, TimeoutError):
        raise LogHistoryReplacementTransportError("logs history replacement transport failed") from None

    if result.returncode != 0:
        raise LogHistoryReplacementTransportError("logs history replacement was rejected")
    return True
