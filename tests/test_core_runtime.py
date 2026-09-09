import json
from collections.abc import Mapping

import pytest

from ha_syncapp.core_runtime import (
    CoreApiResponse,
    CoreApiTransport,
    CoreRuntimeError,
    collect_core_runtime_inventory,
)

TOKEN = "secret-token"
CONFIG_URL = "http://supervisor/core/api/config"
STATES_URL = "http://supervisor/core/api/states"
SERVICES_URL = "http://supervisor/core/api/services"


def _transport(
    records: list[tuple[str, str, Mapping[str, str], float, int]],
) -> CoreApiTransport:
    payloads = {
        CONFIG_URL: {"version": "2026.9.1", "components": ["api"]},
        STATES_URL: [{"entity_id": "light.kitchen", "state": "on"}],
        SERVICES_URL: [{"domain": "light", "services": {}}],
    }

    def send(
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> CoreApiResponse:
        records.append((method, url, headers, timeout, limit))
        return CoreApiResponse(
            200,
            "application/json; charset=utf-8",
            json.dumps(payloads[url]).encode(),
        )

    return send


def test_collects_only_fixed_get_endpoints_and_maps_runtime() -> None:
    records: list[tuple[str, str, Mapping[str, str], float, int]] = []

    inventory = collect_core_runtime_inventory(token=TOKEN, transport=_transport(records))

    assert [(record[0], record[1]) for record in records] == [
        ("GET", CONFIG_URL),
        ("GET", STATES_URL),
        ("GET", SERVICES_URL),
    ]
    assert all(record[2]["Authorization"] == f"Bearer {TOKEN}" for record in records)
    assert all(record[2]["Accept"] == "application/json" for record in records)
    assert inventory.manifest == {
        "core_config": {"version": "2026.9.1", "components": ["api"]},
        "entity_count": 1,
        "service_domain_count": 1,
        "home_assistant_version": "2026.9.1",
    }
    assert inventory.homeassistant["states"] == [
        {"entity_id": "light.kitchen", "state": "on"}
    ]
    assert inventory.homeassistant["services"] == [{"domain": "light", "services": {}}]
    assert "entities" not in inventory.homeassistant
    assert "devices" not in inventory.homeassistant
    assert "integrations" not in inventory.homeassistant


def test_uses_supervisor_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[tuple[str, str, Mapping[str, str], float, int]] = []
    monkeypatch.setenv("SUPERVISOR_TOKEN", TOKEN)

    collect_core_runtime_inventory(transport=_transport(records))

    assert all(record[2]["Authorization"] == f"Bearer {TOKEN}" for record in records)


def test_explicit_token_takes_precedence_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[tuple[str, str, Mapping[str, str], float, int]] = []
    monkeypatch.setenv("SUPERVISOR_TOKEN", "environment-token")

    collect_core_runtime_inventory(token=TOKEN, transport=_transport(records))

    assert all(record[2]["Authorization"] == f"Bearer {TOKEN}" for record in records)


@pytest.mark.parametrize(
    ("status", "content_type", "body", "message"),
    [
        (503, "application/json", b"{}", "request failed"),
        (200, "text/plain", b"{}", "not JSON"),
        (200, "application/json", b"not-json", "invalid JSON"),
        (200, "application/json", b'{"bad":NaN}', "invalid JSON"),
        (200, "application/json", b"[]", "shape is invalid"),
    ],
)
def test_config_failures_are_sanitized(
    status: int,
    content_type: str,
    body: bytes,
    message: str,
) -> None:
    def send(
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> CoreApiResponse:
        return CoreApiResponse(status, content_type, body)

    with pytest.raises(CoreRuntimeError, match=message) as caught:
        collect_core_runtime_inventory(token=TOKEN, transport=send)

    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("bad_url", [STATES_URL, SERVICES_URL])
def test_list_endpoints_reject_unexpected_top_level_shape(bad_url: str) -> None:
    payloads: dict[str, object] = {
        CONFIG_URL: {"version": "2026.9.1"},
        STATES_URL: [],
        SERVICES_URL: [],
    }
    payloads[bad_url] = {}

    def send(
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> CoreApiResponse:
        return CoreApiResponse(200, "application/json", json.dumps(payloads[url]).encode())

    with pytest.raises(CoreRuntimeError, match="shape is invalid"):
        collect_core_runtime_inventory(token=TOKEN, transport=send)


def test_response_size_limit_is_enforced() -> None:
    def send(
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> CoreApiResponse:
        return CoreApiResponse(200, "application/json", b"{}" + b" " * 20)

    with pytest.raises(CoreRuntimeError, match="size limit"):
        collect_core_runtime_inventory(
            token=TOKEN,
            max_response_bytes=8,
            transport=send,
        )


def test_transport_exception_does_not_expose_token() -> None:
    def send(
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> CoreApiResponse:
        raise RuntimeError(f"failed with {headers['Authorization']}")

    with pytest.raises(CoreRuntimeError) as caught:
        collect_core_runtime_inventory(token=TOKEN, transport=send)

    assert TOKEN not in str(caught.value)


def test_missing_or_malformed_token_fails_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    called = False

    def send(
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> CoreApiResponse:
        nonlocal called
        called = True
        return CoreApiResponse(200, "application/json", b"{}")

    with pytest.raises(CoreRuntimeError, match="credential"):
        collect_core_runtime_inventory(transport=send)
    with pytest.raises(CoreRuntimeError, match="credential"):
        collect_core_runtime_inventory(token=" bad ", transport=send)

    assert called is False


@pytest.mark.parametrize(
    ("timeout_seconds", "max_response_bytes"),
    [
        (0, 1024),
        (61, 1024),
        (True, 1024),
        (10, 0),
        (10, 16 * 1024 * 1024 + 1),
    ],
)
def test_invalid_limits_fail_before_transport(
    timeout_seconds: float,
    max_response_bytes: int,
) -> None:
    called = False

    def send(
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> CoreApiResponse:
        nonlocal called
        called = True
        return CoreApiResponse(200, "application/json", b"{}")

    with pytest.raises(CoreRuntimeError):
        collect_core_runtime_inventory(
            token=TOKEN,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            transport=send,
        )

    assert called is False
