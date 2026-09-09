"""Bounded read-only Home Assistant registry collection over Core WebSocket."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Final, Protocol

from websockets.sync.client import connect

from .runtime_inventory import RuntimeInventoryInput

_CORE_WEBSOCKET_URL: Final = "ws://supervisor/core/websocket"
_COMMANDS: Final[tuple[tuple[str, str, str], ...]] = (
    ("entities", "config/entity_registry/list", "entity_id"),
    ("devices", "config/device_registry/list", "id"),
    ("areas", "config/area_registry/list", "id"),
    ("floors", "config/floor_registry/list", "id"),
    ("labels", "config/label_registry/list", "id"),
    ("integrations", "config_entries/get", "entry_id"),
)
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_MESSAGE_BYTES: Final = 4 * 1024 * 1024


class CoreWebSocketRuntimeError(RuntimeError):
    """Read-only Home Assistant registry data could not be collected safely."""


class WebSocketSession(Protocol):
    """Minimal synchronous session boundary used by production transport and tests."""

    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...


SessionFactory = Callable[[str, float, int], AbstractContextManager[WebSocketSession]]


@dataclass(frozen=True, slots=True)
class _RegistryCommand:
    dataset: str
    command_type: str
    identity_key: str


def collect_core_websocket_inventory(
    *,
    token: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
    session_factory: SessionFactory | None = None,
) -> RuntimeInventoryInput:
    """Collect a bounded allowlist of read-only registry datasets."""
    bearer = _resolve_token(token)
    _validate_limits(timeout_seconds, max_message_bytes)
    factory = session_factory or _default_session_factory

    try:
        with factory(_CORE_WEBSOCKET_URL, timeout_seconds, max_message_bytes) as session:
            _authenticate(
                session=session,
                token=bearer,
                timeout_seconds=timeout_seconds,
                max_message_bytes=max_message_bytes,
            )
            datasets: dict[str, object] = {}
            for request_id, command_data in enumerate(_COMMANDS, start=1):
                command = _RegistryCommand(*command_data)
                datasets[command.dataset] = _collect_registry(
                    session=session,
                    command=command,
                    request_id=request_id,
                    timeout_seconds=timeout_seconds,
                    max_message_bytes=max_message_bytes,
                )
    except CoreWebSocketRuntimeError:
        raise
    except Exception:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket request failed") from None

    return RuntimeInventoryInput(
        manifest={
            "registry_entity_count": len(_require_list(datasets.get("entities"))),
            "registry_device_count": len(_require_list(datasets.get("devices"))),
            "area_count": len(_require_list(datasets.get("areas"))),
            "floor_count": len(_require_list(datasets.get("floors"))),
            "label_count": len(_require_list(datasets.get("labels"))),
            "integration_config_entry_count": len(_require_list(datasets.get("integrations"))),
        },
        homeassistant=datasets,
    )


def _authenticate(
    *,
    session: WebSocketSession,
    token: str,
    timeout_seconds: float,
    max_message_bytes: int,
) -> None:
    required = _receive_json(session, timeout_seconds, max_message_bytes)
    if not isinstance(required, dict) or required.get("type") != "auth_required":
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket authentication failed")

    _send_json(session, {"type": "auth", "access_token": token}, max_message_bytes)
    response = _receive_json(session, timeout_seconds, max_message_bytes)
    if not isinstance(response, dict) or response.get("type") != "auth_ok":
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket authentication failed")


def _collect_registry(
    *,
    session: WebSocketSession,
    command: _RegistryCommand,
    request_id: int,
    timeout_seconds: float,
    max_message_bytes: int,
) -> list[dict[str, object]]:
    if command.command_type not in {entry[1] for entry in _COMMANDS}:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket command is not allowed")
    _send_json(
        session,
        {"id": request_id, "type": command.command_type},
        max_message_bytes,
    )
    response = _receive_json(session, timeout_seconds, max_message_bytes)
    if not isinstance(response, dict):
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket result is invalid")
    if response.get("type") != "result" or response.get("id") != request_id:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket result is invalid")
    if response.get("success") is not True:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket command failed")
    return _normalize_registry(response.get("result"), command.identity_key)


def _normalize_registry(value: object, identity_key: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket registry is invalid")
    normalized: list[dict[str, object]] = []
    identities: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict) or any(not isinstance(key, str) for key in entry):
            raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket registry is invalid")
        identity = entry.get(identity_key)
        if not isinstance(identity, str) or not identity or identity in identities:
            raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket registry is invalid")
        identities.add(identity)
        normalized.append(dict(entry))
    normalized.sort(key=lambda item: str(item[identity_key]))
    return normalized


def _receive_json(
    session: WebSocketSession,
    timeout_seconds: float,
    max_message_bytes: int,
) -> object:
    try:
        message = session.recv(timeout=timeout_seconds)
    except Exception:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket receive failed") from None
    if isinstance(message, bytes):
        if len(message) > max_message_bytes:
            raise CoreWebSocketRuntimeError(
                "Home Assistant Core WebSocket message exceeds size limit"
            )
        try:
            text = message.decode("utf-8")
        except UnicodeDecodeError:
            raise CoreWebSocketRuntimeError(
                "Home Assistant Core WebSocket message is invalid"
            ) from None
    elif isinstance(message, str):
        if len(message.encode("utf-8")) > max_message_bytes:
            raise CoreWebSocketRuntimeError(
                "Home Assistant Core WebSocket message exceeds size limit"
            )
        text = message
    else:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket message is invalid")
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise CoreWebSocketRuntimeError(
            "Home Assistant Core WebSocket message is invalid"
        ) from None


def _send_json(
    session: WebSocketSession,
    payload: Mapping[str, object],
    max_message_bytes: int,
) -> None:
    try:
        message = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        raise CoreWebSocketRuntimeError(
            "Home Assistant Core WebSocket command is invalid"
        ) from None
    if len(message.encode("utf-8")) > max_message_bytes:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket command exceeds size limit")
    try:
        session.send(message)
    except Exception:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket send failed") from None


def _default_session_factory(
    url: str,
    timeout_seconds: float,
    max_message_bytes: int,
) -> AbstractContextManager[WebSocketSession]:
    if url != _CORE_WEBSOCKET_URL:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket boundary is invalid")
    return connect(
        url,
        open_timeout=timeout_seconds,
        close_timeout=timeout_seconds,
        max_size=max_message_bytes,
    )


def _resolve_token(token: str | None) -> str:
    candidate = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if not isinstance(candidate, str) or not candidate.strip() or candidate != candidate.strip():
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket credential is unavailable")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket credential is invalid")
    return candidate


def _validate_limits(timeout_seconds: float, max_message_bytes: int) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket timeout is invalid")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket timeout is invalid")
    if type(max_message_bytes) is not int or not 0 < max_message_bytes <= 16 * 1024 * 1024:
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket message limit is invalid")


def _require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise CoreWebSocketRuntimeError("Home Assistant Core WebSocket registry is invalid")
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")
