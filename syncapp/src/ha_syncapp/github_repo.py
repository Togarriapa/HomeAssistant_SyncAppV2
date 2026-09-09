"""Fail-closed GitHub metadata verification for the configured private Repo B."""

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

MAX_METADATA_BYTES = 65536
REQUEST_TIMEOUT_SECONDS = 10.0
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class RepositoryVerificationError(RuntimeError):
    """Repo B could not be proven private and identical to the configured target."""


@dataclass(frozen=True)
class RepoIdentity:
    target: str
    repository_id: int


@dataclass(frozen=True)
class BranchHead:
    target: str
    repository_id: int
    branch: str
    commit_sha: str


@dataclass(frozen=True)
class BranchAbsence:
    """Evidence that one exact branch was absent after Repo B identity verification."""

    target: str
    repository_id: int
    branch: str


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RepositoryVerificationError("GitHub returned invalid repository metadata")
        result[key] = value
    return result


def _metadata_url(target: str) -> str:
    parts = target.split("/")
    if len(parts) != 2 or not all(parts):
        raise RepositoryVerificationError("Configured repository target is invalid")
    owner, repository = parts
    return f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"


def _validate_branch(branch: str) -> None:
    if not isinstance(branch, str) or _BRANCH.fullmatch(branch) is None:
        raise RepositoryVerificationError("Configured repository branch is invalid")
    if (
        branch in {".", ".."}
        or branch.startswith("/")
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise RepositoryVerificationError("Configured repository branch is invalid")


def _read_json(request: Request, *, allow_not_found: bool = False) -> object | None:
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            raw = response.read(MAX_METADATA_BYTES + 1)
    except HTTPError as error:
        if allow_not_found and error.code == 404:
            return None
        raise RepositoryVerificationError(
            f"GitHub repository verification failed with HTTP {error.code}"
        ) from None
    except (URLError, TimeoutError, OSError):
        raise RepositoryVerificationError(
            "GitHub repository verification transport failed"
        ) from None

    if len(raw) > MAX_METADATA_BYTES:
        raise RepositoryVerificationError("GitHub repository metadata exceeded the size limit")
    try:
        parsed: object = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        return parsed
    except (UnicodeError, ValueError, RecursionError):
        raise RepositoryVerificationError("GitHub returned invalid repository metadata") from None


def _request(url: str, token: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "HomeAssistant-SyncAppV2",
        },
        method="GET",
    )


def fetch_and_verify_private_repository(
    target: str,
    token: str,
    *,
    expected_id: int | None = None,
) -> RepoIdentity:
    """Fetch authenticated GitHub metadata and prove Repo B is the intended private repo."""
    metadata = _read_json(_request(_metadata_url(target), token))
    if not isinstance(metadata, dict):
        raise RepositoryVerificationError("GitHub returned invalid repository metadata")

    repository_id = metadata.get("id")
    full_name = metadata.get("full_name")
    private = metadata.get("private")
    if type(repository_id) is not int or repository_id <= 0:
        raise RepositoryVerificationError("GitHub returned invalid repository metadata")
    if not isinstance(full_name, str) or full_name.casefold() != target.casefold():
        raise RepositoryVerificationError("GitHub repository identity does not match configuration")
    if private is not True:
        raise RepositoryVerificationError("Configured repository is not private")
    if expected_id is not None and repository_id != expected_id:
        raise RepositoryVerificationError("Configured repository identity changed unexpectedly")
    return RepoIdentity(target=full_name, repository_id=repository_id)


def fetch_optional_trusted_branch_head(
    target: str,
    token: str,
    *,
    expected_id: int,
    branch: str = "main",
) -> BranchHead | BranchAbsence:
    """Return trusted branch presence or identity-bound absence after verifying Repo B."""
    if type(expected_id) is not int or expected_id <= 0:
        raise RepositoryVerificationError("Expected repository identity is invalid")
    _validate_branch(branch)
    identity = fetch_and_verify_private_repository(target, token, expected_id=expected_id)
    branch_url = f"{_metadata_url(identity.target)}/branches/{quote(branch, safe='')}"
    metadata = _read_json(_request(branch_url, token), allow_not_found=True)
    if metadata is None:
        return BranchAbsence(
            target=identity.target,
            repository_id=identity.repository_id,
            branch=branch,
        )
    if not isinstance(metadata, dict):
        raise RepositoryVerificationError("GitHub returned invalid branch metadata")
    name = metadata.get("name")
    commit = metadata.get("commit")
    if name != branch or not isinstance(commit, dict):
        raise RepositoryVerificationError("GitHub returned invalid branch metadata")
    commit_sha = commit.get("sha")
    if not isinstance(commit_sha, str) or _COMMIT_SHA.fullmatch(commit_sha) is None:
        raise RepositoryVerificationError("GitHub returned invalid branch metadata")
    return BranchHead(
        target=identity.target,
        repository_id=identity.repository_id,
        branch=name,
        commit_sha=commit_sha,
    )


def fetch_trusted_branch_head(
    target: str,
    token: str,
    *,
    expected_id: int,
    branch: str = "main",
) -> BranchHead:
    """Read one exact Repo B branch head only after re-proving repository identity."""
    state = fetch_optional_trusted_branch_head(
        target,
        token,
        expected_id=expected_id,
        branch=branch,
    )
    if isinstance(state, BranchAbsence):
        raise RepositoryVerificationError("Trusted repository branch does not exist")
    return state
