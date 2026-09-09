"""Read-only Home Assistant Core runtime collection through the Supervisor proxy."""

from __future__ import annotations

import http.client
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from .runtime_inventory import RuntimeInventoryInput

_CORE_API_ROOT: Final = "http://supervisor/core/api"
_ENDPOINTS: Final[tuple[tuple[str, str, type[object]], ...]] = (
    ("config", f"{_CORE_API_ROOT}/config", dict),
    ("states", f"{_CORE_API_ROOT}/states", list),
    ("services", f"{_CORE_API_ROOT}/services", list),
)
_ALLOWED_PATHS: Final = {
    f"{_CORE_API_ROOT}/config": "/core/api/config",
    f"{_CORE_API_ROOT}/states": "/core/api/states",
    f"{_CORE_API_ROOT}/services": "/core/api/services",
}
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024


class CoreRuntimeError(RuntimeError):
    """Read-only Home Assistant Core runtime data could not be collected safely."""


@dataclass(frozen=True, slots=True)
class CoreApiResponse:
    """Bounded transport response used by the collector and its tests."""

    status: int
    content_type: str
    body: bytes


CoreApiTransport = Callable[[str, str, Mapping[str, str], float, int], CoreApiResponse]


def collect_core_runtime_inventory(
    *,
    token: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    transport: CoreApiTransport | None = None,
) -> RuntimeInventoryInput:
    """Collect documented GET-only Core runtime datasets into runtime inventory input."""
    bearer = _resolve_token(token)
    _validate_limits(timeout_seconds, max_response_bytes)
    sender = transport or _default_transport

    payloads: dict[str, object] = {}
    for name, url, expected_type in _ENDPOINTS:
        payloads[name] = _request_json(
            name=name,
            url=url,
            expected_type=expected_type,
            token=bearer,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            transport=sender,
        )

    config = payloads["config"]
    states = payloads["states"]
    services = payloads["services"]
    if not isinstance(config, dict) or not isinstance(states, list) or not isinstance(services, list):
        raise CoreRuntimeError("Home Assistant Core API response shape is invalid")

    manifest: dict[str, object] = {
        "core_config": config,
        "entity_count": len(states),
        "service_domain_count": len(services),
    }
    version = config.get("version")
    if isinstance(version, str):
        manifest["home_assistant_version"] = version

    return RuntimeInventoryInput(
        manifest=manifest,
        homeassistant={
            "states": states,
            "services": services,
        },
    )


def _request_json(
    *,
    name: str,
    url: str,
    expected_type: type[object],
    token: str,
    timeout_seconds: float,
    max_response_bytes: int,
    transport: CoreApiTransport,
) -> object:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    try:
        response = transport("GET", url, headers, timeout_seconds, max_response_bytes)
    except Exception:
        raise CoreRuntimeError(f"Home Assistant Core {name} request failed") from None

    if type(response) is not CoreApiResponse:
        raise CoreRuntimeError(f"Home Assistant Core {name} response is invalid")
    if response.status != 200:
        raise CoreRuntimeError(f"Home Assistant Core {name} request failed")
    if _media_type(response.content_type) != "application/json":
        raise CoreRuntimeError(f"Home Assistant Core {name} response is not JSON")
    if len(response.body) > max_response_bytes:
        raise CoreRuntimeError(f"Home Assistant Core {name} response exceeds size limit")

    try:
        payload = json.loads(response.body.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise CoreRuntimeError(f"Home Assistant Core {name} response contains invalid JSON") from None
    if not isinstance(payload, expected_type):
        raise CoreRuntimeError(f"Home Assistant Core {name} response shape is invalid")
    return payload


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> CoreApiResponse:
    path = _ALLOWED_PATHS.get(url)
    if method != "GET" or path is None:
        raise CoreRuntimeError("Home Assistant Core request boundary is invalid")

    connection = http.client.HTTPConnection("supervisor", 80, timeout=timeout_seconds)
    try:
        connection.request("GET", path, headers=dict(headers))
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "") or ""
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > max_response_bytes:
                    raise CoreRuntimeError("Home Assistant Core response exceeds size limit")
            except ValueError:
                raise CoreRuntimeError("Home Assistant Core response metadata is invalid") from None
        body = response.read(max_response_bytes + 1)
        status = int(response.status)
    except CoreRuntimeError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        raise CoreRuntimeError("Home Assistant Core request failed") from None
    finally:
        connection.close()
    return CoreApiResponse(status=status, content_type=content_type, body=body)


def _resolve_token(token: str | None) -> str:
    candidate = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if not isinstance(candidate, str) or not candidate.strip() or candidate != candidate.strip():
        raise CoreRuntimeError("Home Assistant Core API credential is unavailable")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        raise CoreRuntimeError("Home Assistant Core API credential is invalid")
    return candidate


def _validate_limits(timeout_seconds: float, max_response_bytes: int) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise CoreRuntimeError("Home Assistant Core API timeout is invalid")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise CoreRuntimeError("Home Assistant Core API timeout is invalid")
    if type(max_response_bytes) is not int or not 0 < max_response_bytes <= 16 * 1024 * 1024:
        raise CoreRuntimeError("Home Assistant Core API response limit is invalid")


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")
