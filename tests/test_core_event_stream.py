from __future__ import annotations

import json
from collections.abc import Mapping
from types import TracebackType

import pytest
from ha_syncapp.core_event_stream import CoreEventStreamError, consume_core_runtime_events
from ha_syncapp.runtime_event_trigger import runtime_event_types


class _FakeSocket:
    def __init__(self, messages: list[str | bytes]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []

    async def recv(self) -> str | bytes:
        if not self.messages:
            raise RuntimeError("test socket exhausted")
        return self.messages.pop(0)

    async def send(self, message: str) -> None:
        self.sent.append(message)


class _FakeContext:
    def __init__(self, socket: _FakeSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> _FakeSocket:
        return self.socket

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def _json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _success_messages() -> list[str | bytes]:
    messages: list[str | bytes] = [
        _json({"type": "auth_required", "ha_version": "2026.9.0"}),
        _json({"type": "auth_ok", "ha_version": "2026.9.0"}),
    ]
    for command_id, _event_type in enumerate(runtime_event_types(), start=1):
        messages.append(
            _json({"id": command_id, "type": "result", "success": True, "result": None})
        )
    return messages


def _connector_for(socket: _FakeSocket):
    def connector(url: str, open_timeout: float, close_timeout: float, max_size: int):
        assert url == "ws://supervisor/core/websocket"
        assert open_timeout == 10.0
        assert close_timeout == 5.0
        assert max_size == 1024 * 1024
        return _FakeContext(socket)

    return connector


@pytest.mark.asyncio
async def test_subscriber_authenticates_subscribes_and_normalizes_event() -> None:
    event_types = runtime_event_types()
    state_id = event_types.index("state_changed") + 1
    messages = _success_messages()
    messages.append(
        _json(
            {
                "id": state_id,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {"secret": "must-not-reach-handler"},
                },
            }
        )
    )
    socket = _FakeSocket(messages)
    received: list[Mapping[str, object]] = []

    async def handler(event: Mapping[str, object]) -> None:
        received.append(event)

    count = await consume_core_runtime_events(
        handler,
        token="token-sentinel",
        max_events=1,
        connector=_connector_for(socket),
    )

    assert count == 1
    assert received == [{"event_type": "state_changed"}]
    sent = [json.loads(message) for message in socket.sent]
    assert sent[0] == {"access_token": "token-sentinel", "type": "auth"}
    subscriptions = sent[1:]
    assert [item["event_type"] for item in subscriptions] == list(event_types)
    assert all(item["type"] == "subscribe_events" for item in subscriptions)


@pytest.mark.asyncio
async def test_event_may_arrive_during_subscription_setup_and_is_not_forwarded() -> None:
    event_types = runtime_event_types()
    first_type = event_types[0]
    messages = [
        _json({"type": "auth_required"}),
        _json({"type": "auth_ok"}),
        _json(
            {
                "id": 1,
                "type": "event",
                "event": {"event_type": first_type, "data": {"ignored": True}},
            }
        ),
    ]
    for command_id, _event_type in enumerate(event_types, start=1):
        messages.append(_json({"id": command_id, "type": "result", "success": True}))
    messages.append(_json({"id": 1, "type": "event", "event": {"event_type": first_type}}))
    socket = _FakeSocket(messages)
    received: list[Mapping[str, object]] = []

    async def handler(event: Mapping[str, object]) -> None:
        received.append(event)

    await consume_core_runtime_events(
        handler,
        token="token",
        max_events=1,
        connector=_connector_for(socket),
    )
    assert received == [{"event_type": first_type}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        [_json({"type": "auth_invalid", "message": "no"})],
        [_json({"type": "auth_required"}), _json({"type": "auth_invalid"})],
        [b"binary"],
        ["not-json"],
    ],
)
async def test_authentication_and_message_failures_are_sanitized(messages: list[str | bytes]) -> None:
    socket = _FakeSocket(messages)

    async def handler(event: Mapping[str, object]) -> None:
        raise AssertionError(event)

    with pytest.raises(CoreEventStreamError) as error:
        await consume_core_runtime_events(
            handler,
            token="secret-sentinel",
            max_events=1,
            connector=_connector_for(socket),
        )
    assert "secret-sentinel" not in str(error.value)


@pytest.mark.asyncio
async def test_failed_subscription_fails_closed() -> None:
    messages = [
        _json({"type": "auth_required"}),
        _json({"type": "auth_ok"}),
        _json({"id": 1, "type": "result", "success": False, "error": {"message": "secret"}}),
    ]
    socket = _FakeSocket(messages)

    async def handler(event: Mapping[str, object]) -> None:
        raise AssertionError(event)

    with pytest.raises(CoreEventStreamError, match="subscription failed"):
        await consume_core_runtime_events(
            handler,
            token="token",
            max_events=1,
            connector=_connector_for(socket),
        )


@pytest.mark.asyncio
async def test_unknown_subscription_id_fails_closed() -> None:
    messages = [
        _json({"type": "auth_required"}),
        _json({"type": "auth_ok"}),
        _json({"id": 999, "type": "result", "success": True}),
    ]
    socket = _FakeSocket(messages)

    async def handler(event: Mapping[str, object]) -> None:
        raise AssertionError(event)

    with pytest.raises(CoreEventStreamError, match="subscription state is invalid"):
        await consume_core_runtime_events(
            handler,
            token="token",
            max_events=1,
            connector=_connector_for(socket),
        )


@pytest.mark.asyncio
async def test_mismatched_event_type_fails_closed() -> None:
    messages = _success_messages()
    messages.append(
        _json({"id": 1, "type": "event", "event": {"event_type": "wrong-event"}})
    )
    socket = _FakeSocket(messages)

    async def handler(event: Mapping[str, object]) -> None:
        raise AssertionError(event)

    with pytest.raises(CoreEventStreamError, match="event message is invalid"):
        await consume_core_runtime_events(
            handler,
            token="token",
            max_events=1,
            connector=_connector_for(socket),
        )


@pytest.mark.asyncio
async def test_oversized_text_message_fails_closed() -> None:
    socket = _FakeSocket(["x" * 65])

    async def handler(event: Mapping[str, object]) -> None:
        raise AssertionError(event)

    with pytest.raises(CoreEventStreamError, match="exceeds size limit"):
        await consume_core_runtime_events(
            handler,
            token="token",
            max_events=1,
            max_message_bytes=64,
            connector=lambda url, opened, closed, size: _FakeContext(socket),
        )


@pytest.mark.asyncio
async def test_invalid_limits_fail_before_connecting() -> None:
    connected = False

    def connector(url: str, opened: float, closed: float, size: int):
        nonlocal connected
        connected = True
        raise AssertionError((url, opened, closed, size))

    async def handler(event: Mapping[str, object]) -> None:
        raise AssertionError(event)

    with pytest.raises(CoreEventStreamError, match="count limit is invalid"):
        await consume_core_runtime_events(
            handler,
            token="token",
            max_events=0,
            connector=connector,
        )
    assert connected is False
