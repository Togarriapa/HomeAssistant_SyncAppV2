from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest
from ha_syncapp.core_websocket_runtime import (
    CoreWebSocketRuntimeError,
    collect_core_websocket_inventory,
)


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self._responses = iter(responses)
        self.sent: list[dict[str, object]] = []

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout: float | None = None) -> str:
        del timeout
        response = next(self._responses)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            return response
        return json.dumps(response)


def _success_responses() -> list[object]:
    return [
        {"type": "auth_required", "ha_version": "2026.9.0"},
        {"type": "auth_ok", "ha_version": "2026.9.0"},
        {
            "id": 1,
            "type": "result",
            "success": True,
            "result": [
                {"entity_id": "switch.z", "device_id": "device-z"},
                {"entity_id": "light.a", "device_id": "device-a"},
            ],
        },
        {
            "id": 2,
            "type": "result",
            "success": True,
            "result": [
                {"id": "device-z", "name": "Z"},
                {"id": "device-a", "name": "A"},
            ],
        },
        {
            "id": 3,
            "type": "result",
            "success": True,
            "result": [
                {"id": "kitchen", "name": "Kitchen"},
                {"id": "attic", "name": "Attic"},
            ],
        },
    ]


def _factory_for(
    session: FakeSession,
    observed: dict[str, object] | None = None,
):
    @contextmanager
    def factory(url: str, timeout: float, max_bytes: int) -> Iterator[FakeSession]:
        if observed is not None:
            observed.update(url=url, timeout=timeout, max_bytes=max_bytes)
        yield session

    return factory


def _records(value: object) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], value)


def test_collects_only_allowlisted_registries_deterministically() -> None:
    session = FakeSession(_success_responses())
    observed: dict[str, object] = {}

    inventory = collect_core_websocket_inventory(
        token="supervisor-token",
        timeout_seconds=7,
        max_message_bytes=8192,
        session_factory=_factory_for(session, observed),
    )

    assert observed == {
        "url": "ws://supervisor/core/websocket",
        "timeout": 7,
        "max_bytes": 8192,
    }
    assert session.sent == [
        {"access_token": "supervisor-token", "type": "auth"},
        {"id": 1, "type": "config/entity_registry/list"},
        {"id": 2, "type": "config/device_registry/list"},
        {"id": 3, "type": "config/area_registry/list"},
    ]
    assert inventory.manifest == {
        "registry_entity_count": 2,
        "registry_device_count": 2,
        "area_count": 2,
    }
    assert [entry["entity_id"] for entry in _records(inventory.homeassistant["entities"])] == [
        "light.a",
        "switch.z",
    ]
    assert [entry["id"] for entry in _records(inventory.homeassistant["devices"])] == [
        "device-a",
        "device-z",
    ]
    assert [entry["id"] for entry in _records(inventory.homeassistant["areas"])] == [
        "attic",
        "kitchen",
    ]


def test_rejects_authentication_failure_without_leaking_token() -> None:
    session = FakeSession([{"type": "auth_required"}, {"type": "auth_invalid"}])

    with pytest.raises(CoreWebSocketRuntimeError) as exc_info:
        collect_core_websocket_inventory(
            token="sensitive-token",
            session_factory=_factory_for(session),
        )

    assert "authentication failed" in str(exc_info.value)
    assert "sensitive-token" not in str(exc_info.value)


def test_rejects_unexpected_authentication_protocol_shape() -> None:
    session = FakeSession([{"type": "event"}])

    with pytest.raises(CoreWebSocketRuntimeError, match="authentication failed"):
        collect_core_websocket_inventory(
            token="token",
            session_factory=_factory_for(session),
        )


def test_rejects_result_identifier_mismatch() -> None:
    responses = _success_responses()
    responses[2] = {"id": 99, "type": "result", "success": True, "result": []}

    with pytest.raises(CoreWebSocketRuntimeError, match="result is invalid"):
        collect_core_websocket_inventory(
            token="token",
            session_factory=_factory_for(FakeSession(responses)),
        )


def test_rejects_unsuccessful_command_result() -> None:
    responses = _success_responses()
    responses[2] = {
        "id": 1,
        "type": "result",
        "success": False,
        "error": {"code": "unauthorized", "message": "secret detail"},
    }

    with pytest.raises(CoreWebSocketRuntimeError) as exc_info:
        collect_core_websocket_inventory(
            token="token",
            session_factory=_factory_for(FakeSession(responses)),
        )

    assert str(exc_info.value) == "Home Assistant Core WebSocket command failed"
    assert "secret detail" not in str(exc_info.value)


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        {"id": 1, "type": "event", "success": True, "result": []},
        {"id": 1, "type": "result", "success": True, "result": {}},
        {
            "id": 1,
            "type": "result",
            "success": True,
            "result": [{"entity_id": "duplicate"}, {"entity_id": "duplicate"}],
        },
    ],
)
def test_rejects_invalid_registry_protocol(response: Any) -> None:
    responses = _success_responses()
    responses[2] = response

    with pytest.raises(CoreWebSocketRuntimeError):
        collect_core_websocket_inventory(
            token="token",
            session_factory=_factory_for(FakeSession(responses)),
        )


def test_rejects_oversized_message() -> None:
    session = FakeSession(['{"type":"auth_required","padding":"xxxxxxxx"}'])

    with pytest.raises(CoreWebSocketRuntimeError, match="exceeds size limit"):
        collect_core_websocket_inventory(
            token="token",
            max_message_bytes=20,
            session_factory=_factory_for(session),
        )


def test_sanitizes_transport_failure() -> None:
    session = FakeSession([RuntimeError("transport included secret-token")])

    with pytest.raises(CoreWebSocketRuntimeError) as exc_info:
        collect_core_websocket_inventory(
            token="secret-token",
            session_factory=_factory_for(session),
        )

    assert str(exc_info.value) == "Home Assistant Core WebSocket receive failed"
    assert "secret-token" not in str(exc_info.value)


def test_rejects_invalid_limits_before_opening_transport() -> None:
    opened = False

    @contextmanager
    def factory(url: str, timeout: float, max_bytes: int) -> Iterator[FakeSession]:
        nonlocal opened
        opened = True
        del url, timeout, max_bytes
        yield FakeSession([])

    with pytest.raises(CoreWebSocketRuntimeError, match="timeout is invalid"):
        collect_core_websocket_inventory(
            token="token",
            timeout_seconds=0,
            session_factory=factory,
        )
    assert opened is False
