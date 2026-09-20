"""Bounded GitHub ref transport for an authorized deployment promotion."""

from __future__ import annotations

import json
from typing import NoReturn, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .deployment_promotion import DeploymentPromotion, PromotionRemoteState
from .github_repo import (
    MAX_METADATA_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    RepositoryVerificationError,
    fetch_and_verify_private_repository,
    fetch_trusted_branch_head,
)


class DeploymentPromotionTransportError(RuntimeError):
    """GitHub promotion transport failed without exposing response details."""


def read_promotion_remote_state(
    target: str, token: str, repository_id: int, tag: str
) -> PromotionRemoteState:
    """Re-prove repository identity and read the exact candidate/main/tag refs."""
    try:
        identity = fetch_and_verify_private_repository(target, token, expected_id=repository_id)
        candidate = fetch_trusted_branch_head(
            identity.target, token, expected_id=repository_id, branch="candidate"
        )
        main = fetch_trusted_branch_head(
            identity.target, token, expected_id=repository_id, branch="main"
        )
        tag_sha = _fetch_tag(identity.target, token, tag)
        result = PromotionRemoteState(candidate.commit_sha, main.commit_sha, tag_sha)
        result.validate()
        return result
    except DeploymentPromotionTransportError:
        raise
    except (RepositoryVerificationError, AttributeError, ValueError):
        raise DeploymentPromotionTransportError("promotion remote state is unavailable") from None


def publish_promotion_refs(
    intent: DeploymentPromotion, state: PromotionRemoteState, token: str
) -> None:
    """Publish only missing authorized refs, never forcing an existing ref."""
    try:
        intent.validate()
        state.validate()
        identity = fetch_and_verify_private_repository(
            intent.target, token, expected_id=intent.repository_id
        )
        if state.candidate_sha != intent.candidate_sha:
            _invalid()
        if state.main_sha == intent.baseline_sha:
            _write_ref(
                identity.target,
                token,
                f"heads/{quote('main', safe='')}",
                intent.candidate_sha,
                create=False,
            )
        elif state.main_sha != intent.candidate_sha:
            _invalid()
        if state.tag_sha is None:
            _write_ref(
                identity.target,
                token,
                f"tags/{quote(intent.known_good_tag, safe='')}",
                intent.candidate_sha,
                create=True,
            )
        elif state.tag_sha != intent.candidate_sha:
            _invalid()
    except DeploymentPromotionTransportError:
        raise
    except (RepositoryVerificationError, AttributeError, ValueError):
        raise DeploymentPromotionTransportError("promotion publication is unavailable") from None


def _fetch_tag(target: str, token: str, tag: str) -> str | None:
    owner, repository = target.split("/", 1)
    url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/git/ref/tags/{quote(tag, safe='')}"
    )
    value = _request_json(url, token, "GET", None, allow_not_found=True)
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("ref") != f"refs/tags/{tag}":
        _invalid()
    target_object = value.get("object")
    if not isinstance(target_object, dict) or target_object.get("type") != "commit":
        _invalid()
    sha = target_object.get("sha")
    if not isinstance(sha, str):
        _invalid()
    return sha


def _write_ref(
    target: str,
    token: str,
    ref_suffix: str,
    sha: str,
    *,
    create: bool,
) -> None:
    owner, repository = target.split("/", 1)
    base = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/git/refs"
    )
    if create:
        url = base
        payload: object = {"ref": f"refs/{ref_suffix}", "sha": sha}
        method = "POST"
    else:
        url = f"{base}/{ref_suffix}"
        payload = {"force": False, "sha": sha}
        method = "PATCH"
    value = _request_json(url, token, method, payload)
    if not isinstance(value, dict):
        _invalid()
    expected_ref = f"refs/{ref_suffix}"
    target_object = value.get("object")
    if (
        value.get("ref") != expected_ref
        or not isinstance(target_object, dict)
        or target_object.get("sha") != sha
    ):
        _invalid()


def _request_json(
    url: str,
    token: str,
    method: str,
    payload: object | None,
    *,
    allow_not_found: bool = False,
) -> object | None:
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("ascii")
    request = Request(
        url,
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "HomeAssistant-SyncAppV2",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            raw = response.read(MAX_METADATA_BYTES + 1)
    except HTTPError as error:
        if allow_not_found and error.code == 404:
            return None
        raise DeploymentPromotionTransportError(
            f"promotion GitHub request failed with HTTP {error.code}"
        ) from None
    except (URLError, TimeoutError, OSError):
        raise DeploymentPromotionTransportError(
            "promotion GitHub transport is unavailable"
        ) from None
    if len(raw) > MAX_METADATA_BYTES:
        _invalid()
    try:
        return cast(object, json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object))
    except (UnicodeError, ValueError, RecursionError):
        _invalid()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _invalid() -> NoReturn:
    raise DeploymentPromotionTransportError("promotion GitHub state is invalid") from None
