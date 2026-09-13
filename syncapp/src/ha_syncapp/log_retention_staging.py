"""Isolated acquisition of exact generated-log history for authorized rebuilding."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess  # nosec B404
import uuid
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote

from .log_history_evidence import TrustedLogHistoryEvidence

_TOKEN = re.compile(r"^[!-~]{1,512}$")
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FETCH_REF = "refs/syncapp/log-retention"


class LogRetentionStagingError(RuntimeError):
    """Exact logs history could not be acquired into isolated private state."""


def prepare_log_history_staging(
    *,
    evidence: TrustedLogHistoryEvidence,
    staging_root: Path,
    token: str,
) -> Path:
    """Fetch one complete exact-head logs history into a disposable private Git repository."""

    _validate_evidence(evidence)
    token_value = _validate_token(token)
    root = _prepare_root(staging_root)
    repository = root / f".log-retention-{uuid.uuid4().hex}.tmp"
    try:
        repository.mkdir(mode=0o700)
        executable = _git_executable()
        _run(executable, repository, ("init", "--initial-branch", "logs"))
        askpass, token_file = _create_askpass(repository, token_value)
        try:
            owner, name = evidence.target.split("/", 1)
            remote = f"https://github.com/{quote(owner, safe='')}/{quote(name, safe='')}.git"
            _run(
                executable,
                repository,
                (
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    remote,
                    f"+{evidence.expected_head_sha}:{_FETCH_REF}",
                ),
                askpass=askpass,
            )
            fetched = _run(
                executable,
                repository,
                ("rev-parse", "--verify", f"{_FETCH_REF}^{{commit}}"),
            )
            if fetched != evidence.expected_head_sha:
                raise LogRetentionStagingError("logs retention staging head is inconsistent")
            for record in evidence.commits:
                _run(executable, repository, ("cat-file", "-e", f"{record.sha}^{{commit}}"))
        finally:
            with suppress(OSError):
                askpass.unlink(missing_ok=True)
            with suppress(OSError):
                token_file.unlink(missing_ok=True)
        return repository.resolve(strict=True)
    except LogRetentionStagingError:
        shutil.rmtree(repository, ignore_errors=True)
        raise
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(repository, ignore_errors=True)
        raise LogRetentionStagingError("logs retention staging transport failed") from None


def _validate_evidence(evidence: TrustedLogHistoryEvidence) -> None:
    target_parts = evidence.target.split("/") if isinstance(evidence.target, str) else []
    if (
        type(evidence) is not TrustedLogHistoryEvidence
        or evidence.branch != "logs"
        or not evidence.commits
        or evidence.commits[0].sha != evidence.expected_head_sha
        or len(target_parts) != 2
        or not all(target_parts)
        or type(evidence.repository_id) is not int
        or evidence.repository_id <= 0
        or _COMMIT_SHA.fullmatch(evidence.expected_head_sha) is None
    ):
        raise LogRetentionStagingError("logs retention staging evidence is invalid")


def _prepare_root(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise LogRetentionStagingError("logs retention staging root is invalid")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
            raise LogRetentionStagingError("logs retention staging root is unsafe")
        os.chmod(path, 0o700)
        return path.resolve(strict=True)
    except OSError:
        raise LogRetentionStagingError("logs retention staging root is unavailable") from None


def _validate_token(token: str) -> str:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise LogRetentionStagingError("GitHub authentication is invalid")
    return token


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise LogRetentionStagingError("Git executable is unavailable")
    return executable


def _create_askpass(repository: Path, token: str) -> tuple[Path, Path]:
    helper = repository / ".syncapp-askpass"
    token_file = repository / ".syncapp-askpass.token"
    script = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *) IFS= read -r token < "${0}.token"; printf '%s\\n' "$token" ;;
esac
"""
    try:
        token_descriptor = os.open(
            token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(token_descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        helper_descriptor = os.open(
            helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700
        )
        with os.fdopen(helper_descriptor, "w", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        with suppress(OSError):
            helper.unlink(missing_ok=True)
        with suppress(OSError):
            token_file.unlink(missing_ok=True)
        raise LogRetentionStagingError(
            "logs retention authentication could not be prepared"
        ) from None
    return helper, token_file


def _run(
    executable: str,
    repository: Path,
    arguments: tuple[str, ...],
    *,
    askpass: Path | None = None,
) -> str:
    environment = {
        "PATH": os.path.dirname(executable),
        "HOME": str(repository),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "LC_ALL": "C",
    }
    if askpass is not None:
        environment["GIT_ASKPASS"] = str(askpass)
    command = (
        executable,
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        *arguments,
    )
    try:
        result = subprocess.run(  # nosec B603
            command,
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=30,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError, TimeoutError):
        raise LogRetentionStagingError("logs retention staging transport failed") from None
    if result.returncode != 0:
        raise LogRetentionStagingError("logs retention staging command failed")
    return result.stdout.strip()
