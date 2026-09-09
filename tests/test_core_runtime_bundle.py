from __future__ import annotations

import pytest
from ha_syncapp import core_runtime_bundle
from ha_syncapp.core_runtime import CoreRuntimeError
from ha_syncapp.core_websocket_runtime import CoreWebSocketRuntimeError
from ha_syncapp.runtime_inventory import RuntimeInventoryInput


def _rest_inventory() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={
            "home_assistant_version": "2026.9.1",
            "entity_count": 2,
        },
        homeassistant={
            "states": [{"entity_id": "light.one"}],
            "services": [{"domain": "light"}],
        },
    )


def _registry_inventory() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={
            "registry_entity_count": 2,
            "registry_device_count": 1,
            "area_count": 1,
        },
        homeassistant={
            "entities": [{"entity_id": "light.one"}, {"entity_id": "switch.two"}],
            "devices": [{"id": "device-one"}],
            "areas": [{"id": "kitchen"}],
        },
    )


def test_collects_rest_then_registry_data_into_one_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def collect_rest(*, token: str | None = None) -> RuntimeInventoryInput:
        assert token == "core-token"
        events.append("rest")
        return _rest_inventory()

    def collect_registries(*, token: str | None = None) -> RuntimeInventoryInput:
        assert token == "core-token"
        events.append("registries")
        return _registry_inventory()

    monkeypatch.setattr(core_runtime_bundle, "collect_core_runtime_inventory", collect_rest)
    monkeypatch.setattr(
        core_runtime_bundle,
        "collect_core_websocket_inventory",
        collect_registries,
    )

    inventory = core_runtime_bundle.collect_core_runtime_bundle(token="core-token")

    assert events == ["rest", "registries"]
    assert inventory.manifest == {
        "home_assistant_version": "2026.9.1",
        "entity_count": 2,
        "registry_entity_count": 2,
        "registry_device_count": 1,
        "area_count": 1,
    }
    assert inventory.homeassistant == {
        "states": [{"entity_id": "light.one"}],
        "services": [{"domain": "light"}],
        "entities": [{"entity_id": "light.one"}, {"entity_id": "switch.two"}],
        "devices": [{"id": "device-one"}],
        "areas": [{"id": "kitchen"}],
    }


def test_rest_failure_is_sanitized_and_stops_before_registry_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_called = False

    def fail_rest(*, token: str | None = None) -> RuntimeInventoryInput:
        raise CoreRuntimeError(f"REST failed with {token}")

    def collect_registries(*, token: str | None = None) -> RuntimeInventoryInput:
        nonlocal registry_called
        registry_called = True
        return _registry_inventory()

    monkeypatch.setattr(core_runtime_bundle, "collect_core_runtime_inventory", fail_rest)
    monkeypatch.setattr(
        core_runtime_bundle,
        "collect_core_websocket_inventory",
        collect_registries,
    )

    with pytest.raises(core_runtime_bundle.CoreRuntimeBundleError) as exc_info:
        core_runtime_bundle.collect_core_runtime_bundle(token="secret-token")

    assert str(exc_info.value) == "Home Assistant Core runtime collection failed"
    assert "secret-token" not in str(exc_info.value)
    assert registry_called is False


def test_registry_failure_is_sanitized_without_returning_partial_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core_runtime_bundle,
        "collect_core_runtime_inventory",
        lambda **kwargs: _rest_inventory(),
    )

    def fail_registries(*, token: str | None = None) -> RuntimeInventoryInput:
        raise CoreWebSocketRuntimeError(f"registry failed with {token}")

    monkeypatch.setattr(
        core_runtime_bundle,
        "collect_core_websocket_inventory",
        fail_registries,
    )

    with pytest.raises(core_runtime_bundle.CoreRuntimeBundleError) as exc_info:
        core_runtime_bundle.collect_core_runtime_bundle(token="secret-token")

    assert str(exc_info.value) == "Home Assistant Core runtime collection failed"
    assert "secret-token" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            RuntimeInventoryInput(manifest={"count": 1}),
            RuntimeInventoryInput(manifest={"count": 2}),
        ),
        (
            RuntimeInventoryInput(manifest={}, homeassistant={"states": []}),
            RuntimeInventoryInput(manifest={}, homeassistant={"states": []}),
        ),
        (
            RuntimeInventoryInput(manifest={}, supervisor={"system": {}}),
            RuntimeInventoryInput(manifest={}, supervisor={"system": {}}),
        ),
        (
            RuntimeInventoryInput(manifest={}, deployments={"a" * 40: {}}),
            RuntimeInventoryInput(manifest={}, deployments={"a" * 40: {}}),
        ),
    ],
)
def test_merge_rejects_duplicate_dataset_keys(
    first: RuntimeInventoryInput,
    second: RuntimeInventoryInput,
) -> None:
    with pytest.raises(core_runtime_bundle.CoreRuntimeBundleError, match="overlap"):
        core_runtime_bundle.merge_runtime_inventory_inputs(first, second)


def test_merge_preserves_non_overlapping_sections() -> None:
    first = RuntimeInventoryInput(
        manifest={"one": 1},
        supervisor={"system": {"healthy": True}},
        analysis={"topology": {"nodes": []}},
    )
    second = RuntimeInventoryInput(
        manifest={"two": 2},
        hardware={"storage": {"free": 123}},
        deployments={"a" * 40: {"status": "success"}},
    )

    merged = core_runtime_bundle.merge_runtime_inventory_inputs(first, second)

    assert merged.manifest == {"one": 1, "two": 2}
    assert merged.supervisor == first.supervisor
    assert merged.analysis == first.analysis
    assert merged.hardware == second.hardware
    assert merged.deployments == second.deployments
