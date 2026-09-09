"""Fail-closed GitHub metadata verification for the configured private Repo B."""

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

MAX_METADATA_BYTES = 65536
REQUEST_TIMEOUT_SECONDS = 10.0


class RepositoryVerificationError(RuntimeError):
    """Repo B could not be proven private and identical to the configured target."""


@dataclass(frozen=True)
class RepoIdentity:
    target: str
    repository_id: int


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


def fetch_and_verify_private_repository(
    target: str,
    token: str,
    *,
    expected_id: int | None = None,
) -> RepoIdentity:
    """Fetch authenticated GitHub metadata and prove Repo B is the intended private repo."""
    request = Request(
        _metadata_url(target),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "HomeAssistant-SyncAppV2",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            raw = response.read(MAX_METADATA_BYTES + 1)
    except HTTPError as error:
        raise RepositoryVerificationError(
            f"GitHub repository verification failed with HTTP {error.code}"
        ) from None
    except (URLError, TimeoutError, OSError):
        raise RepositoryVerificationError("GitHub repository verification transport failed") from None

    if len(raw) > MAX_METADATA_BYTES:
        raise RepositoryVerificationError("GitHub repository metadata exceeded the size limit")
    try:
        metadata = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise RepositoryVerificationError("GitHub returned invalid repository metadata") from None
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
