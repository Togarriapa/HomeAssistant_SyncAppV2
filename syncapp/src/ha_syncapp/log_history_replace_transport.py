from __future__ import annotations

import math
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
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
    """Raised when logs history cannot be rebuilt or published safely."""


@dataclass(frozen=True, slots=True, init=False)
class LogHistoryReplacementArtifact:
    """Builder-produced replacement history bound to one authorization."""

    repository: Path
    target: str
    repository_id: int
    branch: str
    expected_head_sha: str
    retained_shas: tuple[str, ...]
    replacement_head_sha: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "LogHistoryReplacementArtifact must be produced by "
            "build_log_history_replacement()"
        )


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
        encoding="utf-8",
        errors="strict",
        timeout=timeout,
    )


def build_log_history_replacement(
    *,
    authorization: LogHistoryReplacementAuthorization,
    repository: Path,
    timeout: float = 30.0,
    runner: CommandRunner = _run_git,
) -> LogHistoryReplacementArtifact:
    """Rebuild retained commits while severing only authorized pruned ancestry."""

    _validate_authorization_boundary(authorization)
    if not authorization.requires_replacement:
        raise LogHistoryReplacementTransportError(
            "logs history replacement is not required"
        )
    repository = _validate_repository_path(repository)
    timeout_value = _validate_timeout(timeout)
    executable = _git_executable()

    rebuilt_parent: str | None = None
    retained_oldest_first = tuple(reversed(authorization.retained_shas))
    for index, original_sha in enumerate(retained_oldest_first):
        expected_original_parent = (
            authorization.pruned_shas[0]
            if index == 0
            else retained_oldest_first[index - 1]
        )
        raw_commit = _read_commit(
            executable=executable,
            repository=repository,
            commit_sha=original_sha,
            timeout=timeout_value,
            runner=runner,
        )
        payload = _rewrite_commit_parent(
            raw_commit,
            expected_original_parent=expected_original_parent,
            rebuilt_parent=rebuilt_parent,
        )
        rebuilt_parent = _write_commit_object(
            executable=executable,
            repository=repository,
            payload=payload,
            expected_sha_length=len(authorization.expected_head_sha),
            timeout=timeout_value,
            runner=runner,
        )

    if rebuilt_parent is None or rebuilt_parent == authorization.expected_head_sha:
        raise LogHistoryReplacementTransportError("logs rebuilt history is invalid")
    return _replacement_artifact(
        repository=repository,
        authorization=authorization,
        replacement_head_sha=rebuilt_parent,
    )


def replace_logs_history(
    *,
    authorization: LogHistoryReplacementAuthorization,
    artifact: LogHistoryReplacementArtifact | None = None,
    repository: Path | None = None,
    timeout: float = 30.0,
    runner: CommandRunner = _run_git,
) -> bool:
    """Publish only a validated builder artifact with an exact logs lease."""

    _validate_authorization_boundary(authorization)
    if not authorization.requires_replacement:
        return False
    _validate_artifact(authorization=authorization, artifact=artifact)
    if artifact is None:
        raise LogHistoryReplacementTransportError(
            "logs replacement artifact is invalid"
        )
    if repository is not None:
        supplied = _validate_repository_path(repository)
        if supplied != artifact.repository:
            raise LogHistoryReplacementTransportError(
                "logs replacement artifact is invalid"
            )
    timeout_value = _validate_timeout(timeout)
    _validate_artifact_history(
        authorization=authorization,
        artifact=artifact,
        timeout=timeout_value,
        runner=runner,
    )

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
        f"{artifact.replacement_head_sha}:{ref}",
        f"--force-with-lease={ref}:{authorization.expected_head_sha}",
    )
    try:
        result = runner(command, cwd=artifact.repository, timeout=timeout_value)
    except (OSError, UnicodeError, subprocess.SubprocessError, TimeoutError):
        raise LogHistoryReplacementTransportError(
            "logs history replacement transport failed"
        ) from None
    if not isinstance(result, subprocess.CompletedProcess):
        raise LogHistoryReplacementTransportError(
            "logs history replacement transport failed"
        )
    if result.returncode != 0:
        raise LogHistoryReplacementTransportError(
            "logs history replacement was rejected"
        )
    return True


def _validate_authorization_boundary(
    authorization: LogHistoryReplacementAuthorization,
) -> None:
    if type(authorization) is not LogHistoryReplacementAuthorization:
        raise LogHistoryReplacementTransportError(
            "logs replacement authorization is invalid"
        )
    if authorization.branch != LOG_HISTORY_BRANCH:
        raise LogHistoryReplacementTransportError(
            "history replacement is restricted to logs"
        )
    _validate_repository_identity(authorization)
    all_shas = (*authorization.retained_shas, *authorization.pruned_shas)
    if (
        not isinstance(authorization.expected_head_sha, str)
        or _COMMIT_SHA.fullmatch(authorization.expected_head_sha) is None
        or not authorization.retained_shas
        or authorization.retained_shas[0] != authorization.expected_head_sha
        or any(_COMMIT_SHA.fullmatch(sha) is None for sha in all_shas)
        or any(len(sha) != len(authorization.expected_head_sha) for sha in all_shas)
    ):
        raise LogHistoryReplacementTransportError(
            "logs replacement authorization is invalid"
        )


def _validate_artifact(
    *,
    authorization: LogHistoryReplacementAuthorization,
    artifact: LogHistoryReplacementArtifact | None,
) -> None:
    if (
        type(artifact) is not LogHistoryReplacementArtifact
        or artifact.repository != artifact.repository.resolve()
        or artifact.target.casefold() != authorization.target.casefold()
        or artifact.repository_id != authorization.repository_id
        or artifact.branch != LOG_HISTORY_BRANCH
        or artifact.expected_head_sha != authorization.expected_head_sha
        or artifact.retained_shas != authorization.retained_shas
        or _COMMIT_SHA.fullmatch(artifact.replacement_head_sha) is None
        or len(artifact.replacement_head_sha) != len(authorization.expected_head_sha)
        or artifact.replacement_head_sha == authorization.expected_head_sha
    ):
        raise LogHistoryReplacementTransportError(
            "logs replacement artifact is invalid"
        )


def _validate_artifact_history(
    *,
    authorization: LogHistoryReplacementAuthorization,
    artifact: LogHistoryReplacementArtifact,
    timeout: float,
    runner: CommandRunner,
) -> None:
    executable = _git_executable()
    rebuilt_sha = artifact.replacement_head_sha
    retained = authorization.retained_shas
    for index, original_sha in enumerate(retained):
        original = _read_commit(
            executable=executable,
            repository=artifact.repository,
            commit_sha=original_sha,
            timeout=timeout,
            runner=runner,
        )
        rebuilt = _read_commit(
            executable=executable,
            repository=artifact.repository,
            commit_sha=rebuilt_sha,
            timeout=timeout,
            runner=runner,
        )
        _original_tree, original_parents = _commit_tree_and_parents(original)
        _rebuilt_tree, rebuilt_parents = _commit_tree_and_parents(rebuilt)
        expected_original_parent = (
            retained[index + 1]
            if index + 1 < len(retained)
            else authorization.pruned_shas[0]
        )
        oldest = index + 1 == len(retained)
        if (oldest and rebuilt_parents) or (
            not oldest and len(rebuilt_parents) != 1
        ):
            raise LogHistoryReplacementTransportError(
                "logs replacement artifact is invalid"
            )
        rebuilt_parent = None if oldest else rebuilt_parents[0]
        expected_rebuilt = _rewrite_commit_parent(
            original,
            expected_original_parent=expected_original_parent,
            rebuilt_parent=rebuilt_parent,
        )
        if (
            original_parents != (expected_original_parent,)
            or rebuilt != expected_rebuilt
        ):
            raise LogHistoryReplacementTransportError(
                "logs replacement artifact is invalid"
            )
        if not oldest:
            rebuilt_sha = rebuilt_parents[0]


def _read_commit(
    *,
    executable: str,
    repository: Path,
    commit_sha: str,
    timeout: float,
    runner: CommandRunner,
) -> str:
    command = (
        executable,
        "-c",
        f"core.hooksPath={os.devnull}",
        "cat-file",
        "commit",
        commit_sha,
    )
    try:
        result = runner(command, cwd=repository, timeout=timeout)
    except (OSError, UnicodeError, subprocess.SubprocessError, TimeoutError):
        raise LogHistoryReplacementTransportError(
            "logs retained history could not be read"
        ) from None
    if (
        not isinstance(result, subprocess.CompletedProcess)
        or result.returncode != 0
        or not isinstance(result.stdout, str)
    ):
        raise LogHistoryReplacementTransportError(
            "logs retained history could not be read"
        )
    return result.stdout


def _commit_tree_and_parents(raw_commit: str) -> tuple[str, tuple[str, ...]]:
    header, separator, _message = raw_commit.partition("\n\n")
    if not separator or not header:
        raise LogHistoryReplacementTransportError(
            "logs replacement artifact is invalid"
        )
    trees: list[str] = []
    parents: list[str] = []
    for line in header.split("\n"):
        if line.startswith("tree "):
            trees.append(line[5:])
        elif line.startswith("parent "):
            parents.append(line[7:])
    if (
        len(trees) != 1
        or _COMMIT_SHA.fullmatch(trees[0]) is None
        or any(_COMMIT_SHA.fullmatch(parent) is None for parent in parents)
        or len(parents) > 1
    ):
        raise LogHistoryReplacementTransportError(
            "logs replacement artifact is invalid"
        )
    return trees[0], tuple(parents)


def _rewrite_commit_parent(
    raw_commit: str,
    *,
    expected_original_parent: str,
    rebuilt_parent: str | None,
) -> str:
    header, separator, message = raw_commit.partition("\n\n")
    if not separator or not header:
        raise LogHistoryReplacementTransportError(
            "logs retained commit is invalid"
        )

    groups: list[list[str]] = []
    for line in header.split("\n"):
        if line.startswith(" "):
            if not groups:
                raise LogHistoryReplacementTransportError(
                    "logs retained commit is invalid"
                )
            groups[-1].append(line)
        else:
            groups.append([line])

    tree_groups = [group for group in groups if group[0].startswith("tree ")]
    parent_groups = [group for group in groups if group[0].startswith("parent ")]
    if len(tree_groups) != 1 or len(parent_groups) != 1:
        raise LogHistoryReplacementTransportError(
            "logs retained commit ancestry is invalid"
        )
    tree_sha = tree_groups[0][0][5:]
    parent_sha = parent_groups[0][0][7:]
    if (
        _COMMIT_SHA.fullmatch(tree_sha) is None
        or _COMMIT_SHA.fullmatch(parent_sha) is None
        or parent_sha != expected_original_parent
    ):
        raise LogHistoryReplacementTransportError(
            "logs retained commit ancestry is invalid"
        )

    rebuilt_groups: list[list[str]] = []
    for group in groups:
        key = group[0].split(" ", 1)[0]
        if key in {"parent", "gpgsig", "mergetag"}:
            continue
        rebuilt_groups.append(group)
        if key == "tree" and rebuilt_parent is not None:
            rebuilt_groups.append([f"parent {rebuilt_parent}"])

    rebuilt_header = "\n".join(line for group in rebuilt_groups for line in group)
    return f"{rebuilt_header}\n\n{message}"


def _write_commit_object(
    *,
    executable: str,
    repository: Path,
    payload: str,
    expected_sha_length: int,
    timeout: float,
    runner: CommandRunner,
) -> str:
    try:
        with tempfile.TemporaryDirectory(
            prefix="syncapp-log-history-",
            dir=repository / ".git",
        ) as temporary:
            commit_file = Path(temporary) / "commit"
            commit_file.write_text(payload, encoding="utf-8", newline="\n")
            command = (
                executable,
                "-c",
                f"core.hooksPath={os.devnull}",
                "hash-object",
                "-t",
                "commit",
                "-w",
                str(commit_file),
            )
            result = runner(command, cwd=repository, timeout=timeout)
    except (OSError, UnicodeError, subprocess.SubprocessError, TimeoutError):
        raise LogHistoryReplacementTransportError(
            "logs retained history could not be rebuilt"
        ) from None
    if (
        not isinstance(result, subprocess.CompletedProcess)
        or result.returncode != 0
        or not isinstance(result.stdout, str)
    ):
        raise LogHistoryReplacementTransportError(
            "logs retained history could not be rebuilt"
        )
    replacement_sha = result.stdout.strip()
    if (
        _COMMIT_SHA.fullmatch(replacement_sha) is None
        or len(replacement_sha) != expected_sha_length
    ):
        raise LogHistoryReplacementTransportError("logs rebuilt history is invalid")
    return replacement_sha


def _replacement_artifact(
    *,
    repository: Path,
    authorization: LogHistoryReplacementAuthorization,
    replacement_head_sha: str,
) -> LogHistoryReplacementArtifact:
    artifact = object.__new__(LogHistoryReplacementArtifact)
    object.__setattr__(artifact, "repository", repository)
    object.__setattr__(artifact, "target", authorization.target)
    object.__setattr__(artifact, "repository_id", authorization.repository_id)
    object.__setattr__(artifact, "branch", LOG_HISTORY_BRANCH)
    object.__setattr__(artifact, "expected_head_sha", authorization.expected_head_sha)
    object.__setattr__(artifact, "retained_shas", authorization.retained_shas)
    object.__setattr__(artifact, "replacement_head_sha", replacement_head_sha)
    return artifact


def _validate_repository_identity(
    authorization: LogHistoryReplacementAuthorization,
) -> None:
    parts = (
        authorization.target.split("/")
        if isinstance(authorization.target, str)
        else []
    )
    if (
        len(parts) != 2
        or _REPO_OWNER.fullmatch(parts[0]) is None
        or _REPO_NAME.fullmatch(parts[1]) is None
        or parts[1] in {".", ".."}
        or type(authorization.repository_id) is not int
        or authorization.repository_id <= 0
    ):
        raise LogHistoryReplacementTransportError(
            "logs replacement repository identity is invalid"
        )


def _validate_repository_path(repository: Path) -> Path:
    if not isinstance(repository, Path) or not repository.is_absolute():
        raise LogHistoryReplacementTransportError(
            "logs replacement repository path is invalid"
        )
    try:
        metadata = repository.lstat()
        resolved = repository.resolve(strict=True)
    except OSError:
        raise LogHistoryReplacementTransportError(
            "logs replacement repository path is invalid"
        ) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or repository.is_symlink()
        or not (resolved / ".git").is_dir()
    ):
        raise LogHistoryReplacementTransportError(
            "logs replacement repository path is invalid"
        )
    return resolved


def _validate_timeout(timeout: float) -> float:
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or not 0 < timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise LogHistoryReplacementTransportError(
            "logs replacement timeout is invalid"
        )
    return float(timeout)


def _repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return (
        f"https://github.com/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}.git"
    )


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise LogHistoryReplacementTransportError("Git executable is unavailable")
    return executable
