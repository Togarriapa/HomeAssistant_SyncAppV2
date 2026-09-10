"""Bounded read-only Home Assistant Core runtime event subscription."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Final, Protocol, cast

from websockets.asyncio.client import connect

from .runtime_event_trigger import runtime_event_types

_CORE_WEBSOCKET_URL: Final = "ws://supervisor/core/websocket"
_DEFAULT_OPEN_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_CLOSE_TIMEOUT_SECONDS: Final = 5.0
_DEFAULT_MAX_MESSAGE_BYTES: Final = 1024 * 1024
_MAX_TIMEOUT_SECONDS: Final = 60.0
_MAX_MESSAGE_BYTES: Final = 4 * 1024 * 1024


class CoreEventStreamError(RuntimeError):
    """The read-only Home Assistant Core event stream failed closed."""


class EventSocket(Protocol):
    """Minimum async socket surface required by the event subscriber."""

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


EventSocketContext = AbstractAsyncContextManager[EventSocket]
EventSocketConnector = Callable[[str, float, float, int], EventSocketContext]
RuntimeEventHandler = Callable[[Mapping[str, object]], Awaitable[None]]


async def consume_core_runtime_events(
    handler: RuntimeEventHandler,
    *,
    token: str | None = None,
    max_events: int | None = None,
    open_timeout_seconds: float = _DEFAULT_OPEN_TIMEOUT_SECONDS,
    close_timeout_seconds: float = _DEFAULT_CLOSE_TIMEOUT_SECONDS,
    max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
    connector: EventSocketConnector | None = None,
) -> int:
    """Consume normalized runtime events from the documented Supervisor proxy."""
    if not callable(handler):
        raise CoreEventStreamError("Home Assistant Core event handler is invalid")
    bearer = _resolve_token(token)
    _validate_limits(
        max_events,
        open_timeout_seconds,
        close_timeout_seconds,
        max_message_bytes,
    )
    socket_connector = connector or _default_connector

    try:
        async with socket_connector(
            _CORE_WEBSOCKET_URL,
            open_timeout_seconds,
            close_timeout_seconds,
            max_message_bytes,
        ) as socket:
            await _authenticate(socket, bearer, max_message_bytes)
            subscriptions = await _subscribe(socket, max_message_bytes)
            return await _consume_events(
                socket,
                subscriptions,
                handler,
                max_events,
                max_message_bytes,
            )
    except CoreEventStreamError:
        raise
    except Exception:
        raise CoreEventStreamError("Home Assistant Core event stream failed closed") from None


def _default_connector(
    url: str,
    open_timeout_seconds: float,
    close_timeout_seconds: float,
    max_message_bytes: int,
) -> EventSocketContext:
    if url != _CORE_WEBSOCKET_URL:
        raise CoreEventStreamError("Home Assistant Core event endpoint is invalid")
    connection = connect(
        url,
        open_timeout=open_timeout_seconds,
        close_timeout=close_timeout_seconds,
        max_size=max_message_bytes,
    )
    return cast(EventSocketContext, connection)


async def _authenticate(socket: EventSocket, token: str, max_message_bytes: int) -> None:
    required = _decode_message(await socket.recv(), max_message_bytes)
    if required.get("type") != "auth_required":
        raise CoreEventStreamError("Home Assistant Core event authentication state is invalid")

    await socket.send(_encode_message({"type": "auth", "access_token": token}))
    response = _decode_message(await socket.recv(), max_message_bytes)
    if response.get("type") != "auth_ok":
        raise CoreEventStreamError("Home Assistant Core event authentication failed")


async def _subscribe(socket: EventSocket, max_message_bytes: int) -> dict[int, str]:
    subscriptions = {
        command_id: event_type
        for command_id, event_type in enumerate(runtime_event_types(), start=1)
    }
    for command_id, event_type in subscriptions.items():
        await socket.send(
            _encode_message(
                {
                    "id": command_id,
                    "type": "subscribe_events",
                    "event_type": event_type,
                }
            )
        )

    pending = set(subscriptions)
    while pending:
        message = _decode_message(await socket.recv(), max_message_bytes)
        message_type = message.get("type")
        received_id = message.get("id")
        if type(received_id) is not int or received_id not in subscriptions:
            raise CoreEventStreamError("Home Assistant Core event subscription state is invalid")
        if message_type == "event":
            _validate_event_message(message, subscriptions)
            continue
        if message_type != "result" or received_id not in pending:
            raise CoreEventStreamError("Home Assistant Core event subscription state is invalid")
        if message.get("success") is not True:
            raise CoreEventStreamError("Home Assistant Core event subscription failed")
        pending.remove(received_id)
    return subscriptions


async def _consume_events(
    socket: EventSocket,
    subscriptions: Mapping[int, str],
    handler: RuntimeEventHandler,
    max_events: int | None,
    max_message_bytes: int,
) -> int:
    consumed = 0
    while max_events is None or consumed < max_events:
        message = _decode_message(await socket.recv(), max_message_bytes)
        event_type = _validate_event_message(message, subscriptions)
        await handler({"event_type": event_type})
        consumed += 1
    return consumed


def _validate_event_message(
    message: Mapping[str, object],
    subscriptions: Mapping[int, str],
) -> str:
    command_id = message.get("id")
    if message.get("type") != "event" or type(command_id) is not int:
        raise CoreEventStreamError("Home Assistant Core event message is invalid")
    expected_event_type = subscriptions.get(command_id)
    event = message.get("event")
    if expected_event_type is None or not isinstance(event, Mapping):
        raise CoreEventStreamError("Home Assistant Core event message is invalid")
    event_type = event.get("event_type")
    if event_type != expected_event_type:
        raise CoreEventStreamError("Home Assistant Core event message is invalid")
    return expected_event_type


def _decode_message(message: str | bytes, max_message_bytes: int) -> dict[str, object]:
    if not isinstance(message, str):
        raise CoreEventStreamError("Home Assistant Core event message is invalid")
    encoded = message.encode("utf-8")
    if len(encoded) > max_message_bytes:
        raise CoreEventStreamError("Home Assistant Core event message exceeds size limit")
    try:
        payload = json.loads(message, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise CoreEventStreamError(
            "Home Assistant Core event message contains invalid JSON"
        ) from None
    if not isinstance(payload, dict):
        raise CoreEventStreamError("Home Assistant Core event message is invalid")
    return cast(dict[str, object], payload)


def _encode_message(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _resolve_token(token: str | None) -> str:
    candidate = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if not isinstance(candidate, str) or not candidate.strip() or candidate != candidate.strip():
        raise CoreEventStreamError("Home Assistant Core event credential is unavailable")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        raise CoreEventStreamError("Home Assistant Core event credential is invalid")
    return candidate


def _validate_limits(
    max_events: int | None,
    open_timeout_seconds: float,
    close_timeout_seconds: float,
    max_message_bytes: int,
) -> None:
    if max_events is not None and (type(max_events) is not int or max_events <= 0):
        raise CoreEventStreamError("Home Assistant Core event count limit is invalid")
    for timeout in (open_timeout_seconds, close_timeout_seconds):
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise CoreEventStreamError("Home Assistant Core event timeout is invalid")
        if timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
            raise CoreEventStreamError("Home Assistant Core event timeout is invalid")
    if type(max_message_bytes) is not int or not 0 < max_message_bytes <= _MAX_MESSAGE_BYTES:
        raise CoreEventStreamError("Home Assistant Core event message limit is invalid")


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")
