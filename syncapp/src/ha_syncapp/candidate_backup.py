"""Recoverable Supervisor backup evidence bound to an exact validated candidate."""

from __future__ import annotations

import http.client
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from .candidate_dependencies import CandidateDependencyAnalysis
from .candidate_impact import CandidateImpactAnalysis
from .candidate_integrity import CandidateIntegrity
from .candidate_risk import CandidateRiskClassification
from .candidate_semantics import (
    CandidateSemanticValidation,
    verify_candidate_semantic_validation,
)
from .candidate_stage import CandidateStage
from .candidate_validation import CandidateStaticValidation
from .core_version_evidence import CoreVersionEvidence
from .runtime_inventory import RuntimeInventoryInput

_SUPERVISOR_ROOT: Final = "http://supervisor"
_CREATE_URL: Final = f"{_SUPERVISOR_ROOT}/backups/new/full"
_CREATE_PATH: Final = "/backups/new/full"
_DEFAULT_TIMEOUT_SECONDS: Final = 60.0
_DEFAULT_MAX_RESPONSE_BYTES: Final = 64 * 1024
_BACKUP_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class CandidateBackupError(RuntimeError):
    """A recoverable candidate-bound backup could not be established safely."""


@dataclass(frozen=True, slots=True)
class CandidateBackupEvidence:
    """Verified backup identity bound to the semantic candidate authorization evidence."""

    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    runtime_sha256: str
    risk_level: str
    core_version: str
    backup_slug: str


@dataclass(frozen=True, slots=True)
class SupervisorBackupResponse:
    """Bounded Supervisor transport response used by production and deterministic tests."""

    status: int
    content_type: str
    body: bytes


SupervisorBackupTransport = Callable[
    [str, str, Mapping[str, str], bytes | None, float, int],
    SupervisorBackupResponse,
]


def create_candidate_backup(
    semantic: CandidateSemanticValidation,
    static: CandidateStaticValidation,
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    risk: CandidateRiskClassification,
    runtime: RuntimeInventoryInput,
    version: CoreVersionEvidence,
    *,
    token: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    transport: SupervisorBackupTransport | None = None,
) -> CandidateBackupEvidence:
    """Create and verify one full HA backup only after exact semantic authorization."""
    try:
        _verify_semantic(
            semantic, static, integrity, stage, dependencies, impact, risk, runtime, version
        )
        bearer = _resolve_token(token)
        _validate_limits(timeout_seconds, max_response_bytes)
        sender = transport or _default_transport

        request_body = json.dumps(
            {
                "name": f"SyncApp candidate {semantic.candidate_sha[:12]}",
                "background": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        create_payload = _request_json(
            "POST",
            _CREATE_URL,
            bearer,
            request_body,
            timeout_seconds,
            max_response_bytes,
            sender,
        )
        slug = _backup_slug(create_payload)
        info = _request_json(
            "GET",
            f"{_SUPERVISOR_ROOT}/backups/{slug}/info",
            bearer,
            None,
            timeout_seconds,
            max_response_bytes,
            sender,
        )
        _verify_backup_info(info, slug, semantic.core_version)
        _verify_semantic(
            semantic, static, integrity, stage, dependencies, impact, risk, runtime, version
        )
        return CandidateBackupEvidence(
            target=semantic.target,
            repository_id=semantic.repository_id,
            baseline_sha=semantic.baseline_sha,
            candidate_sha=semantic.candidate_sha,
            stage_manifest_sha256=semantic.stage_manifest_sha256,
            runtime_sha256=semantic.runtime_sha256,
            risk_level=semantic.risk_level,
            core_version=semantic.core_version,
            backup_slug=slug,
        )
    except CandidateBackupError:
        raise
    except Exception:
        raise CandidateBackupError("candidate backup evidence could not be established") from None


def _verify_semantic(
    semantic: CandidateSemanticValidation,
    static: CandidateStaticValidation,
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    risk: CandidateRiskClassification,
    runtime: RuntimeInventoryInput,
    version: CoreVersionEvidence,
) -> None:
    try:
        verify_candidate_semantic_validation(
            semantic,
            static,
            integrity,
            stage,
            dependencies,
            impact,
            risk,
            runtime,
            version,
        )
    except Exception:
        raise CandidateBackupError("successful semantic validation is required") from None


def _request_json(
    method: str,
    url: str,
    token: str,
    body: bytes | None,
    timeout_seconds: float,
    max_response_bytes: int,
    transport: SupervisorBackupTransport,
) -> dict[str, object]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        response = transport(method, url, headers, body, timeout_seconds, max_response_bytes)
    except Exception:
        raise CandidateBackupError("Supervisor backup request failed") from None
    if type(response) is not SupervisorBackupResponse or response.status != 200:
        raise CandidateBackupError("Supervisor backup request failed")
    if _media_type(response.content_type) != "application/json":
        raise CandidateBackupError("Supervisor backup response is not JSON")
    if len(response.body) > max_response_bytes:
        raise CandidateBackupError("Supervisor backup response exceeds size limit")
    try:
        payload = json.loads(
            response.body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CandidateBackupError("Supervisor backup response contains invalid JSON") from None
    if not isinstance(payload, dict):
        raise CandidateBackupError("Supervisor backup response shape is invalid")
    return payload


def _backup_slug(payload: dict[str, object]) -> str:
    if set(payload) != {"slug"}:
        raise CandidateBackupError("Supervisor backup creation response is ambiguous")
    slug = payload.get("slug")
    if not isinstance(slug, str) or not _BACKUP_SLUG.fullmatch(slug):
        raise CandidateBackupError("Supervisor backup identifier is invalid")
    return slug


def _verify_backup_info(payload: dict[str, object], slug: str, core_version: str) -> None:
    if payload.get("slug") != slug:
        raise CandidateBackupError("Supervisor backup identity verification failed")
    if payload.get("type") != "full":
        raise CandidateBackupError("Supervisor backup is not a full backup")
    if payload.get("homeassistant") != core_version:
        raise CandidateBackupError("Supervisor backup Home Assistant version is invalid")
    content = payload.get("content")
    if content is not None:
        if not isinstance(content, dict) or content.get("homeassistant") is not True:
            raise CandidateBackupError("Supervisor backup does not contain Home Assistant")


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
    max_response_bytes: int,
) -> SupervisorBackupResponse:
    if method == "POST" and url == _CREATE_URL:
        path = _CREATE_PATH
    elif method == "GET" and url.startswith(f"{_SUPERVISOR_ROOT}/backups/"):
        suffix = url.removeprefix(_SUPERVISOR_ROOT)
        parts = suffix.split("/")
        if len(parts) != 4 or parts[1] != "backups" or parts[3] != "info":
            raise CandidateBackupError("Supervisor backup request boundary is invalid")
        if not _BACKUP_SLUG.fullmatch(parts[2]):
            raise CandidateBackupError("Supervisor backup request boundary is invalid")
        path = suffix
    else:
        raise CandidateBackupError("Supervisor backup request boundary is invalid")

    connection = http.client.HTTPConnection("supervisor", 80, timeout=timeout_seconds)
    try:
        connection.request(method, path, body=body, headers=dict(headers))
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "") or ""
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > max_response_bytes:
                    raise CandidateBackupError("Supervisor backup response exceeds size limit")
            except ValueError:
                raise CandidateBackupError(
                    "Supervisor backup response metadata is invalid"
                ) from None
        response_body = response.read(max_response_bytes + 1)
        status = int(response.status)
    except CandidateBackupError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        raise CandidateBackupError("Supervisor backup request failed") from None
    finally:
        connection.close()
    return SupervisorBackupResponse(status=status, content_type=content_type, body=response_body)


def _resolve_token(token: str | None) -> str:
    candidate = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if not isinstance(candidate, str) or not candidate.strip() or candidate != candidate.strip():
        raise CandidateBackupError("Supervisor backup credential is unavailable")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        raise CandidateBackupError("Supervisor backup credential is invalid")
    return candidate


def _validate_limits(timeout_seconds: float, max_response_bytes: int) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
        raise CandidateBackupError("Supervisor backup timeout is invalid")
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise CandidateBackupError("Supervisor backup timeout is invalid")
    if type(max_response_bytes) is not int or not 0 < max_response_bytes <= 1024 * 1024:
        raise CandidateBackupError("Supervisor backup response limit is invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()
