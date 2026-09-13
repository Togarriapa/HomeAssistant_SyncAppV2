from __future__ import annotations

import math
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from ha_syncapp.database_history_replacement import DatabaseHistoryReplacementAuthorization
from ha_syncapp.database_retention import DATABASE_BRANCH
from ha_syncapp.github_repo import (
    RepositoryVerificationError,
    fetch_trusted_branch_head,
)

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPO_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_HTTP_STATUS = re.compile(r"HTTP ([0-9]{3})(?:$|\D)")
_MAX_TIMEOUT_SECONDS = 300.0


class DatabaseHistoryReplacementFailureKind(StrEnum):
    """Stable retry-policy classification for a replacement failure."""

    INVALID = "invalid"
    STALE = "stale"
    TRANSIENT = "transient"
    REJECTED = "rejected"


class DatabaseHistoryReplacementTransportError(RuntimeError):
    """Sanitized Recorder-history failure with deterministic retry classification."""

    def __init__(
        self,
        message: str,
        *,
        kind: DatabaseHistoryReplacementFailureKind = (
            DatabaseHistoryReplacementFailureKind.INVALID
        ),
    ) -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind is DatabaseHistoryReplacementFailureKind.TRANSIENT


@dataclass(frozen=True, slots=True, init=False)
class DatabaseHistoryReplacementArtifact:
    """Locally rebuilt history bound to one exact database authorization."""

    repository: Path
    target: str
    repository_id: int
    branch: str
    expected_head_sha: str
    retained_shas: tuple[str, ...]
    replacement_head_sha: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "DatabaseHistoryReplacementArtifact must be produced by "
            "build_database_history_replacement()"
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


def build_database_history_replacement(
    *,
    authorization: DatabaseHistoryReplacementAuthorization,
    repository: Path,
    timeout: float = 30.0,
    runner: CommandRunner = _run_git,
) -> DatabaseHistoryReplacementArtifact:
    """Rebuild authorized retained commits without mutating a branch or remote ref.

    The supplied repository is a SyncApp staging Git repository, never the live Home
    Assistant configuration. Only new commit objects are written under its `.git`
    object database; no local or remote ref is changed by this function.
    """

    _validate_authorization_boundary(authorization)
    if not authorization.requires_replacement:
        raise DatabaseHistoryReplacementTransportError(
            "database history replacement is not required"
        )
    repository = _validate_repository_path(repository)
    timeout_value = _validate_timeout(timeout)
    executable = _git_executable()

    rebuilt_parent: str | None = None
    retained_oldest_first = tuple(reversed(authorization.retained_shas))
    for index, original_sha in enumerate(retained_oldest_first):
        expected_original_parent = (
            authorization.pruned_shas[0] if index == 0 else retained_oldest_first[index - 1]
        )
        raw_commit = _read_commit(
            executable=executable,
            repository=repository,
            commit_sha=original_sha,
            timeout=timeout_value,
            runner=runner,
        )
        rebuilt_payload = _rewrite_commit_parent(
            raw_commit,
            expected_original_parent=expected_original_parent,
            rebuilt_parent=rebuilt_parent,
        )
        rebuilt_parent = _write_commit_object(
            executable=executable,
            repository=repository,
            payload=rebuilt_payload,
            expected_sha_length=len(authorization.expected_head_sha),
            timeout=timeout_value,
            runner=runner,
        )

    if rebuilt_parent is None or rebuilt_parent == authorization.expected_head_sha:
        raise DatabaseHistoryReplacementTransportError("database rebuilt history is invalid")
    return _database_history_replacement_artifact(
        repository=repository,
        authorization=authorization,
        replacement_head_sha=rebuilt_parent,
    )


def replace_database_history(
    *,
    authorization: DatabaseHistoryReplacementAuthorization,
    artifact: DatabaseHistoryReplacementArtifact | None = None,
    token: str | None = None,
    repository: Path | None = None,
    timeout: float = 30.0,
    runner: CommandRunner = _run_git,
) -> bool:
    """Atomically replace only `database` after local and remote re-validation."""

    _validate_authorization_boundary(authorization)
    if not authorization.requires_replacement:
        return False

    _validate_artifact(authorization=authorization, artifact=artifact)
    if artifact is None:
        raise DatabaseHistoryReplacementTransportError("database replacement artifact is invalid")
    if repository is not None:
        supplied_repository = _validate_repository_path(repository)
        if supplied_repository != artifact.repository:
            raise DatabaseHistoryReplacementTransportError(
                "database replacement artifact is invalid"
            )
    timeout_value = _validate_timeout(timeout)
    _validate_artifact_history(
        authorization=authorization,
        artifact=artifact,
        timeout=timeout_value,
        runner=runner,
    )
    if not isinstance(token, str) or not token:
        raise DatabaseHistoryReplacementTransportError(
            "database replacement repository verification failed"
        )

    try:
        current = fetch_trusted_branch_head(
            authorization.target,
            token,
            expected_id=authorization.repository_id,
            branch=DATABASE_BRANCH,
        )
    except RepositoryVerificationError as error:
        raise DatabaseHistoryReplacementTransportError(
            "database replacement repository verification failed",
            kind=_repository_verification_failure_kind(error),
        ) from None
    if (
        current.target.casefold() != authorization.target.casefold()
        or current.repository_id != authorization.repository_id
        or current.branch != DATABASE_BRANCH
        or current.commit_sha != authorization.expected_head_sha
    ):
        raise DatabaseHistoryReplacementTransportError(
            "database history changed before replacement",
            kind=DatabaseHistoryReplacementFailureKind.STALE,
        )

    executable = _git_executable()
    ref = f"refs/heads/{DATABASE_BRANCH}"
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
        raise DatabaseHistoryReplacementTransportError(
            "database history replacement transport failed",
            kind=DatabaseHistoryReplacementFailureKind.TRANSIENT,
        ) from None

    if not isinstance(result, subprocess.CompletedProcess):
        raise DatabaseHistoryReplacementTransportError(
            "database history replacement transport failed"
        )
    if result.returncode != 0:
        raise DatabaseHistoryReplacementTransportError(
            "database history replacement was rejected",
            kind=DatabaseHistoryReplacementFailureKind.REJECTED,
        )
    return True


def _validate_artifact_history(
    *,
    authorization: DatabaseHistoryReplacementAuthorization,
    artifact: DatabaseHistoryReplacementArtifact,
    timeout: float,
    runner: CommandRunner,
) -> None:
    """Prove a proposed replacement contains only the authorized snapshot trees."""

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
        original_tree, original_parents = _commit_tree_and_parents(original)
        rebuilt_tree, rebuilt_parents = _commit_tree_and_parents(rebuilt)
        expected_original_parent = (
            retained[index + 1] if index + 1 < len(retained) else authorization.pruned_shas[0]
        )
        if original_parents != (expected_original_parent,) or rebuilt_tree != original_tree:
            raise DatabaseHistoryReplacementTransportError(
                "database replacement artifact is invalid"
            )
        is_oldest_retained = index + 1 == len(retained)
        if is_oldest_retained:
            if rebuilt_parents:
                raise DatabaseHistoryReplacementTransportError(
                    "database replacement artifact is invalid"
                )
            continue
        if len(rebuilt_parents) != 1:
            raise DatabaseHistoryReplacementTransportError(
                "database replacement artifact is invalid"
            )
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
        raise DatabaseHistoryReplacementTransportError(
            "database retained history could not be read",
            kind=DatabaseHistoryReplacementFailureKind.TRANSIENT,
        ) from None
    if (
        not isinstance(result, subprocess.CompletedProcess)
        or result.returncode != 0
        or not isinstance(result.stdout, str)
    ):
        raise DatabaseHistoryReplacementTransportError(
            "database retained history could not be read"
        )
    return result.stdout


def _commit_tree_and_parents(raw_commit: str) -> tuple[str, tuple[str, ...]]:
    header, separator, _message = raw_commit.partition("\n\n")
    if not separator or not header:
        raise DatabaseHistoryReplacementTransportError("database replacement artifact is invalid")
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
        raise DatabaseHistoryReplacementTransportError("database replacement artifact is invalid")
    return trees[0], tuple(parents)


def _rewrite_commit_parent(
    raw_commit: str,
    *,
    expected_original_parent: str,
    rebuilt_parent: str | None,
) -> str:
    header, separator, message = raw_commit.partition("\n\n")
    if not separator or not header:
        raise DatabaseHistoryReplacementTransportError("database retained commit is invalid")

    groups: list[list[str]] = []
    for line in header.split("\n"):
        if line.startswith(" "):
            if not groups:
                raise DatabaseHistoryReplacementTransportError(
                    "database retained commit is invalid"
                )
            groups[-1].append(line)
        else:
            groups.append([line])

    tree_groups = [group for group in groups if group[0].startswith("tree ")]
    parent_groups = [group for group in groups if group[0].startswith("parent ")]
    if len(tree_groups) != 1 or len(parent_groups) != 1:
        raise DatabaseHistoryReplacementTransportError(
            "database retained commit ancestry is invalid"
        )
    tree_sha = tree_groups[0][0][5:]
    parent_sha = parent_groups[0][0][7:]
    if (
        _COMMIT_SHA.fullmatch(tree_sha) is None
        or _COMMIT_SHA.fullmatch(parent_sha) is None
        or parent_sha != expected_original_parent
    ):
        raise DatabaseHistoryReplacementTransportError(
            "database retained commit ancestry is invalid"
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
    git_dir = repository / ".git"
    try:
        with tempfile.TemporaryDirectory(
            prefix="syncapp-db-history-",
            dir=git_dir,
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
        raise DatabaseHistoryReplacementTransportError(
            "database retained history could not be rebuilt",
            kind=DatabaseHistoryReplacementFailureKind.TRANSIENT,
        ) from None
    if (
        not isinstance(result, subprocess.CompletedProcess)
        or result.returncode != 0
        or not isinstance(result.stdout, str)
    ):
        raise DatabaseHistoryReplacementTransportError(
            "database retained history could not be rebuilt"
        )
    replacement_sha = result.stdout.strip()
    if (
        _COMMIT_SHA.fullmatch(replacement_sha) is None
        or len(replacement_sha) != expected_sha_length
    ):
        raise DatabaseHistoryReplacementTransportError("database rebuilt history is invalid")
    return replacement_sha


def _database_history_replacement_artifact(
    *,
    repository: Path,
    authorization: DatabaseHistoryReplacementAuthorization,
    replacement_head_sha: str,
) -> DatabaseHistoryReplacementArtifact:
    artifact = object.__new__(DatabaseHistoryReplacementArtifact)
    object.__setattr__(artifact, "repository", repository)
    object.__setattr__(artifact, "target", authorization.target)
    object.__setattr__(artifact, "repository_id", authorization.repository_id)
    object.__setattr__(artifact, "branch", DATABASE_BRANCH)
    object.__setattr__(artifact, "expected_head_sha", authorization.expected_head_sha)
    object.__setattr__(artifact, "retained_shas", authorization.retained_shas)
    object.__setattr__(artifact, "replacement_head_sha", replacement_head_sha)
    return artifact


def _validate_artifact(
    *,
    authorization: DatabaseHistoryReplacementAuthorization,
    artifact: DatabaseHistoryReplacementArtifact | None,
) -> None:
    if type(artifact) is not DatabaseHistoryReplacementArtifact:
        raise DatabaseHistoryReplacementTransportError("database replacement artifact is invalid")
    if (
        not isinstance(artifact.target, str)
        or artifact.target.casefold() != authorization.target.casefold()
        or type(artifact.repository_id) is not int
        or artifact.repository_id != authorization.repository_id
        or artifact.branch != DATABASE_BRANCH
        or artifact.expected_head_sha != authorization.expected_head_sha
        or artifact.retained_shas != authorization.retained_shas
        or not isinstance(artifact.replacement_head_sha, str)
        or _COMMIT_SHA.fullmatch(artifact.replacement_head_sha) is None
        or len(artifact.replacement_head_sha) != len(authorization.expected_head_sha)
        or artifact.replacement_head_sha == authorization.expected_head_sha
        or not isinstance(artifact.repository, Path)
    ):
        raise DatabaseHistoryReplacementTransportError("database replacement artifact is invalid")
    resolved = _validate_repository_path(artifact.repository)
    if resolved != artifact.repository:
        raise DatabaseHistoryReplacementTransportError("database replacement artifact is invalid")


def _validate_authorization_boundary(
    authorization: DatabaseHistoryReplacementAuthorization,
) -> None:
    if type(authorization) is not DatabaseHistoryReplacementAuthorization:
        raise DatabaseHistoryReplacementTransportError(
            "database replacement authorization is invalid"
        )
    if authorization.branch != DATABASE_BRANCH:
        raise DatabaseHistoryReplacementTransportError(
            "history replacement is restricted to database"
        )
    _validate_repository_identity(authorization)
    _validate_authorization(authorization)


def _validate_repository_identity(
    authorization: DatabaseHistoryReplacementAuthorization,
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
        raise DatabaseHistoryReplacementTransportError(
            "database replacement repository identity is invalid"
        )


def _validate_authorization(
    authorization: DatabaseHistoryReplacementAuthorization,
) -> None:
    retained = authorization.retained_shas
    pruned = authorization.pruned_shas
    expected = authorization.expected_head_sha
    if (
        not isinstance(expected, str)
        or _COMMIT_SHA.fullmatch(expected) is None
        or not isinstance(retained, tuple)
        or not retained
        or retained[0] != expected
        or not isinstance(pruned, tuple)
        or any(
            not isinstance(sha, str)
            or _COMMIT_SHA.fullmatch(sha) is None
            or len(sha) != len(expected)
            for sha in retained
        )
        or any(
            not isinstance(sha, str)
            or _COMMIT_SHA.fullmatch(sha) is None
            or len(sha) != len(expected)
            for sha in pruned
        )
        or len(set(retained)) != len(retained)
        or len(set(pruned)) != len(pruned)
        or bool(set(retained).intersection(pruned))
    ):
        raise DatabaseHistoryReplacementTransportError(
            "database replacement authorization is invalid"
        )


def _validate_repository_path(repository: Path) -> Path:
    if not isinstance(repository, Path) or not repository.is_absolute():
        raise DatabaseHistoryReplacementTransportError(
            "database replacement repository path is invalid"
        )
    try:
        metadata = repository.lstat()
        resolved = repository.resolve(strict=True)
    except OSError:
        raise DatabaseHistoryReplacementTransportError(
            "database replacement repository path is invalid"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode) or repository.is_symlink():
        raise DatabaseHistoryReplacementTransportError(
            "database replacement repository path is invalid"
        )
    git_metadata_path = resolved / ".git"
    try:
        git_metadata = git_metadata_path.lstat()
    except OSError:
        raise DatabaseHistoryReplacementTransportError(
            "database replacement repository path is invalid"
        ) from None
    if not stat.S_ISDIR(git_metadata.st_mode) or git_metadata_path.is_symlink():
        raise DatabaseHistoryReplacementTransportError(
            "database replacement repository path is invalid"
        )
    return resolved


def _validate_timeout(timeout: float) -> float:
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or not 0 < timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise DatabaseHistoryReplacementTransportError("database replacement timeout is invalid")
    return float(timeout)


def _repository_verification_failure_kind(
    error: RepositoryVerificationError,
) -> DatabaseHistoryReplacementFailureKind:
    message = str(error)
    if "transport failed" in message:
        return DatabaseHistoryReplacementFailureKind.TRANSIENT
    match = _HTTP_STATUS.search(message)
    if match is not None:
        status = int(match.group(1))
        if status in {408, 425, 429} or status >= 500:
            return DatabaseHistoryReplacementFailureKind.TRANSIENT
    return DatabaseHistoryReplacementFailureKind.INVALID


def _repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return f"https://github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise DatabaseHistoryReplacementTransportError("Git executable is unavailable")
    return executable
