from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from types import TracebackType

import pytest
from ha_syncapp.core_event_stream import CoreEventStreamError, consume_core_runtime_events
from ha_syncapp.runtime_event_trigger import runtime_event_types


class _Socket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)

    async def recv(self) -> str:
        if not self.messages:
            raise RuntimeError("test socket exhausted")
        return self.messages.pop(0)

    async def send(self, message: str) -> None:
        del message


class _Context:
    def __init__(self, socket: _Socket) -> None:
        self.socket = socket

    async def __aenter__(self) -> _Socket:
        return self.socket

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def _message(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _subscription_messages() -> list[str]:
    messages = [_message({"type": "auth_required"}), _message({"type": "auth_ok"})]
    for command_id, _event_type in enumerate(runtime_event_types(), start=1):
        messages.append(_message({"id": command_id, "type": "result", "success": True}))
    return messages


def _connector(socket: _Socket):
    def connect(url: str, opened: float, closed: float, size: int) -> _Context:
        assert url == "ws://supervisor/core/websocket"
        assert opened == 10.0
        assert closed == 5.0
        assert size == 1024 * 1024
        return _Context(socket)

    return connect


def test_ready_runs_once_before_first_forwarded_event() -> None:
    event_types = runtime_event_types()
    event_type = event_types[0]
    event_id = 1
    socket = _Socket(
        [
            *_subscription_messages(),
            _message({"id": event_id, "type": "event", "event": {"event_type": event_type}}),
        ]
    )
    order: list[str] = []

    async def ready() -> None:
        order.append("ready")

    async def handler(event: Mapping[str, object]) -> None:
        assert event == {"event_type": event_type}
        order.append("event")

    count = asyncio.run(
        consume_core_runtime_events(
            handler,
            on_ready=ready,
            token="token",
            max_events=1,
            connector=_connector(socket),
        )
    )

    assert count == 1
    assert order == ["ready", "event"]


def test_subscription_failure_never_signals_ready() -> None:
    socket = _Socket(
        [
            _message({"type": "auth_required"}),
            _message({"type": "auth_ok"}),
            _message({"id": 1, "type": "result", "success": False}),
        ]
    )
    ready_called = False

    async def ready() -> None:
        nonlocal ready_called
        ready_called = True

    async def handler(event: Mapping[str, object]) -> None:
        raise AssertionError(event)

    with pytest.raises(CoreEventStreamError, match="subscription failed"):
        asyncio.run(
            consume_core_runtime_events(
                handler,
                on_ready=ready,
                token="token",
                max_events=1,
                connector=_connector(socket),
            )
        )
    assert ready_called is False


def test_ready_failure_is_sanitized_and_prevents_event_forwarding() -> None:
    event_types = runtime_event_types()
    socket = _Socket(
        [
            *_subscription_messages(),
            _message({"id": 1, "type": "event", "event": {"event_type": event_types[0]}}),
        ]
    )
    forwarded = False

    async def ready() -> None:
        raise RuntimeError("secret-sentinel")

    async def handler(event: Mapping[str, object]) -> None:
        nonlocal forwarded
        forwarded = True
        raise AssertionError(event)

    with pytest.raises(CoreEventStreamError) as error:
        asyncio.run(
            consume_core_runtime_events(
                handler,
                on_ready=ready,
                token="token",
                max_events=1,
                connector=_connector(socket),
            )
        )
    assert "secret-sentinel" not in str(error.value)
    assert forwarded is False
