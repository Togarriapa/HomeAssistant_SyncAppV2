"""Acquire one trusted Repo B candidate commit into isolated Git metadata only."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess  # nosec B404
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .candidate_detection import CandidateObservation

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TOKEN = re.compile(r"^[!-~]{1,512}$")
_CANDIDATE_BRANCH = "candidate"
_FETCH_REF = "refs/syncapp/candidate-fetch"
_WORKSPACE_PREFIX = ".git-workspace-candidate-"


class CandidateFetchError(RuntimeError):
    """The trusted candidate could not be fetched into isolated metadata safely."""


@dataclass(frozen=True, slots=True)
class CandidateFetch:
    """Evidence that one exact candidate commit exists in isolated Git metadata."""

    root: Path
    target: str
    repository_id: int
    branch: str
    commit_sha: str
    git_ref: str


def fetch_trusted_candidate(
    observation: CandidateObservation,
    expected_sha: str,
    token: str,
    workspace_root: Path,
) -> CandidateFetch:
    """Fetch only the observed candidate branch and prove its exact commit identity."""
    _validate_observation(observation, expected_sha)
    _validate_token(token)
    parent = _trusted_workspace_root(workspace_root)
    executable = _git_executable()

    root = parent / f"{_WORKSPACE_PREFIX}{uuid.uuid4().hex}.tmp"
    try:
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
    except OSError as exc:
        raise CandidateFetchError("candidate fetch workspace could not be created") from exc

    askpass: Path | None = None
    accepted = False
    try:
        _run_git(executable, root, ("init", "--quiet"))
        _verify_initialized_workspace(root)
        askpass = _create_askpass(root)
        refspec = f"+refs/heads/{_CANDIDATE_BRANCH}:{_FETCH_REF}"
        _run_git(
            executable,
            root,
            (
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--depth=1",
                _repository_url(observation.target),
                refspec,
            ),
            token=token,
            askpass=askpass,
        )
        fetched = _run_git(
            executable,
            root,
            ("rev-parse", "--verify", f"{_FETCH_REF}^{{commit}}"),
        )
        if fetched != expected_sha or fetched != observation.commit_sha:
            raise CandidateFetchError("fetched candidate does not match trusted observation")
        object_type = _run_git(executable, root, ("cat-file", "-t", f"{_FETCH_REF}^{{commit}}"))
        if object_type != "commit":
            raise CandidateFetchError("fetched candidate object is not a commit")
        _delete_askpass(askpass)
        askpass = None
        _verify_initialized_workspace(root)
        accepted = True
        return CandidateFetch(
            root=root,
            target=observation.target,
            repository_id=observation.repository_id,
            branch=observation.branch,
            commit_sha=fetched,
            git_ref=_FETCH_REF,
        )
    except CandidateFetchError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateFetchError("candidate fetch failed") from exc
    finally:
        if askpass is not None:
            _delete_askpass(askpass)
        if not accepted:
            shutil.rmtree(root, ignore_errors=True)


def _validate_observation(observation: CandidateObservation, expected_sha: str) -> None:
    if type(observation) is not CandidateObservation:
        raise CandidateFetchError("trusted candidate observation is invalid")
    parts = observation.target.split("/") if isinstance(observation.target, str) else []
    if len(parts) != 2 or not all(parts):
        raise CandidateFetchError("trusted candidate repository target is invalid")
    if type(observation.repository_id) is not int or observation.repository_id <= 0:
        raise CandidateFetchError("trusted candidate repository identity is invalid")
    if observation.branch != _CANDIDATE_BRANCH:
        raise CandidateFetchError("trusted candidate branch is invalid")
    if (
        not isinstance(observation.commit_sha, str)
        or _COMMIT_SHA.fullmatch(observation.commit_sha) is None
    ):
        raise CandidateFetchError("trusted candidate commit is invalid")
    if not isinstance(expected_sha, str) or _COMMIT_SHA.fullmatch(expected_sha) is None:
        raise CandidateFetchError("expected candidate commit is invalid")
    if expected_sha != observation.commit_sha:
        raise CandidateFetchError("expected candidate commit does not match trusted observation")


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise CandidateFetchError("GitHub authentication is invalid")


def _trusted_workspace_root(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CandidateFetchError("candidate workspace root is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise CandidateFetchError("candidate workspace root is unsafe")
    return path.resolve(strict=True)


def _repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return f"https://github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise CandidateFetchError("Git executable is unavailable")
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
        raise CandidateFetchError("Git authentication helper could not be prepared") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_uid != os.geteuid():
        raise CandidateFetchError("Git authentication helper is unsafe")
    return path


def _delete_askpass(path: Path) -> None:
    with suppress(OSError):
        path.unlink(missing_ok=True)


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
        "-c",
        "protocol.file.allow=never",
        "-c",
        "fetch.fsckObjects=true",
        *arguments,
    ]


def _run_git(
    executable: str,
    root: Path,
    arguments: tuple[str, ...],
    *,
    token: str | None = None,
    askpass: Path | None = None,
) -> str:
    try:
        result = subprocess.run(  # nosec B603
            _command(executable, arguments),
            cwd=root,
            env=_git_environment(executable, root, token=token, askpass=askpass),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateFetchError("confined Git command could not execute") from exc
    if result.returncode != 0:
        raise CandidateFetchError("confined Git command failed")
    return result.stdout.strip()


def _verify_initialized_workspace(root: Path) -> None:
    try:
        metadata = root.lstat()
        entries = {entry.name for entry in os.scandir(root)}
        git_metadata = (root / ".git").lstat()
    except OSError as exc:
        raise CandidateFetchError("candidate fetch workspace is invalid") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or root.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or entries != {".git"}
        or not stat.S_ISDIR(git_metadata.st_mode)
        or (root / ".git").is_symlink()
        or git_metadata.st_uid != os.geteuid()
    ):
        raise CandidateFetchError("candidate fetch workspace is invalid")
