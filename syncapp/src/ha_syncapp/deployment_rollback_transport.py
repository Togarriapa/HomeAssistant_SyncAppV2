"""Bounded read-only proofs used by automatic deployment rollback recovery."""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, NoReturn

from .deployment_rollback import (
    DeploymentRollbackError,
    RollbackBackupProof,
    RollbackRepositoryProof,
)
from .github_repo import RepositoryVerificationError, fetch_trusted_branch_head

_SUPERVISOR_ROOT: Final = "http://supervisor"
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_RESPONSE_BYTES: Final = 64 * 1024
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CORE = re.compile(r"^20[0-9]{2}\.(?:[1-9]|1[0-2])\.(?:0|[1-9][0-9]*)$")
_TOKEN = re.compile(r"^[!-~]{1,512}$")


class DeploymentRollbackTransportError(DeploymentRollbackError):
    """A rollback proof transport failed without exposing response details."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class SupervisorBackupProofResponse:
    status: int
    content_type: str
    body: bytes


SupervisorBackupProofTransport = Callable[
    [str, str, Mapping[str, str], bytes, float, int], SupervisorBackupProofResponse
]


def read_rollback_repository_proof(
    target: str, token: str, repository_id: int
) -> RollbackRepositoryProof:
    """Re-prove the exact private Repo B identity and its current main head."""
    try:
        head = fetch_trusted_branch_head(
            target,
            token,
            expected_id=repository_id,
            branch="main",
        )
        proof = RollbackRepositoryProof(head.repository_id, True, head.commit_sha)
        proof.validate()
        return proof
    except DeploymentRollbackError:
        raise DeploymentRollbackTransportError(
            "rollback repository proof is invalid", transient=False
        ) from None
    except (RepositoryVerificationError, AttributeError, ValueError):
        raise DeploymentRollbackTransportError(
            "rollback repository proof is unavailable", transient=True
        ) from None


def read_rollback_backup_proof(
    slug: str,
    token: str,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    transport: SupervisorBackupProofTransport | None = None,
) -> RollbackBackupProof:
    """Read and validate one exact full Supervisor backup without mutation."""
    if (
        not isinstance(slug, str)
        or _SLUG.fullmatch(slug) is None
        or not isinstance(token, str)
        or _TOKEN.fullmatch(token) is None
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or not 0 < timeout_seconds <= 60
        or type(max_response_bytes) is not int
        or not 0 < max_response_bytes <= 1024 * 1024
    ):
        _invalid()
    sender = transport or _default_backup_proof_transport
    try:
        response = sender(
            "GET",
            f"{_SUPERVISOR_ROOT}/backups/{slug}/info",
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            b"",
            timeout_seconds,
            max_response_bytes,
        )
    except DeploymentRollbackTransportError:
        raise
    except Exception:
        raise DeploymentRollbackTransportError(
            "rollback backup proof is unavailable", transient=True
        ) from None
    if (
        type(response) is not SupervisorBackupProofResponse
        or response.status != 200
        or _media_type(response.content_type) != "application/json"
        or len(response.body) > max_response_bytes
    ):
        _invalid()
    try:
        value = json.loads(
            response.body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        _invalid()
    if not isinstance(value, dict):
        _invalid()
    version = value.get("homeassistant")
    content = value.get("content")
    included = content is None or (
        isinstance(content, dict) and content.get("homeassistant") is True
    )
    if (
        value.get("slug") != slug
        or value.get("type") != "full"
        or not isinstance(version, str)
        or _CORE.fullmatch(version) is None
        or not included
    ):
        _invalid()
    proof = RollbackBackupProof(slug, "full", version, True, True)
    proof.validate()
    return proof


def _default_backup_proof_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
) -> SupervisorBackupProofResponse:
    suffix = url.removeprefix(_SUPERVISOR_ROOT)
    parts = suffix.split("/")
    if (
        method != "GET"
        or body != b""
        or not url.startswith(_SUPERVISOR_ROOT)
        or len(parts) != 4
        or parts[1] != "backups"
        or _SLUG.fullmatch(parts[2]) is None
        or parts[3] != "info"
    ):
        _invalid()
    connection = http.client.HTTPConnection("supervisor", 80, timeout=timeout_seconds)
    try:
        connection.request("GET", suffix, headers=dict(headers))
        response = connection.getresponse()
        length = response.getheader("Content-Length")
        if length is not None and int(length) > max_response_bytes:
            _invalid()
        response_body = response.read(max_response_bytes + 1)
        return SupervisorBackupProofResponse(
            int(response.status),
            response.getheader("Content-Type", "") or "",
            response_body,
        )
    except DeploymentRollbackTransportError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        raise DeploymentRollbackTransportError(
            "rollback backup proof is unavailable", transient=True
        ) from None
    finally:
        connection.close()


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    _invalid()


def _invalid() -> NoReturn:
    raise DeploymentRollbackTransportError(
        "rollback proof response is invalid", transient=False
    ) from None
