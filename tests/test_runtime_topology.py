from __future__ import annotations

import copy

import pytest

from ha_syncapp.runtime_inventory import RuntimeInventoryInput
from ha_syncapp.runtime_topology import RuntimeTopologyError, build_runtime_topology


def _inventory() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={"home_assistant_version": "2026.9.1"},
        homeassistant={
            "entities": [
                {
                    "entity_id": "sensor.orphaned",
                    "device_id": "missing-device",
                    "config_entry_id": "entry-missing",
                    "labels": ["label-missing"],
                },
                {
                    "entity_id": "light.kitchen",
                    "device_id": "device-1",
                    "config_entry_id": "entry-1",
                    "labels": ["label-1"],
                },
            ],
            "devices": [
                {
                    "id": "device-1",
                    "area_id": "area-1",
                    "config_entries": ["entry-1"],
                    "labels": ["label-1"],
                }
            ],
            "integrations": [{"entry_id": "entry-1", "domain": "hue"}],
            "areas": [{"id": "area-1", "floor_id": "floor-1", "labels": ["label-1"]}],
            "floors": [{"id": "floor-1"}],
            "labels": [{"id": "label-1"}],
            "states": [
                {"entity_id": "sensor.orphaned", "state": "unavailable"},
                {"entity_id": "light.kitchen", "state": "on"},
            ],
            "services": [{"domain": "switch"}, {"domain": "light"}],
        },
    )


def _edge_key(item: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        item["source_type"],
        item["source_id"],
        item["relation"],
        item["target_type"],
        item["target_id"],
    )


def test_builds_deterministic_registry_topology_and_dependency_evidence() -> None:
    inventory = _inventory()
    original = copy.deepcopy(inventory)

    result = build_runtime_topology(inventory)

    assert inventory == original
    assert result.manifest == {
        "topology_entity_count": 2,
        "topology_device_count": 1,
        "topology_unresolved_count": 3,
    }
    topology = result.analysis["topology"]
    assert topology["schema_version"] == 1
    assert topology["authority"] == {
        "derived_relationships": "best_effort",
        "registry_relationships": "authoritative",
        "runtime_state": "observational",
    }
    assert topology["edges"] == sorted(topology["edges"], key=_edge_key)

    dependencies = result.analysis["dependencies"]
    kitchen = next(
        item for item in dependencies["entities"] if item["entity_id"] == "light.kitchen"
    )
    assert kitchen == {
        "area_id": "area-1",
        "area_source": "derived_via_device",
        "available": True,
        "config_entry_id": "entry-1",
        "device_id": "device-1",
        "entity_id": "light.kitchen",
        "labels": ["label-1"],
        "state": "on",
    }
    orphaned = next(
        item for item in dependencies["entities"] if item["entity_id"] == "sensor.orphaned"
    )
    assert orphaned["available"] is False
    assert dependencies["service_domains"] == ["light", "switch"]
    assert dependencies["unresolved"] == [
        {
            "source_id": "sensor.orphaned",
            "source_type": "entity",
            "relation": "belongs_to_device",
            "target_id": "missing-device",
            "target_type": "device",
        },
        {
            "source_id": "sensor.orphaned",
            "source_type": "entity",
            "relation": "configured_by",
            "target_id": "entry-missing",
            "target_type": "integration",
        },
        {
            "source_id": "sensor.orphaned",
            "source_type": "entity",
            "relation": "has_label",
            "target_id": "label-missing",
            "target_type": "label",
        },
    ]


def test_result_is_independent_of_registry_ordering() -> None:
    first = _inventory()
    second_homeassistant = {
        key: list(reversed(value)) for key, value in first.homeassistant.items()
    }
    second = RuntimeInventoryInput(manifest=first.manifest, homeassistant=second_homeassistant)

    assert build_runtime_topology(first) == build_runtime_topology(second)


@pytest.mark.parametrize(
    ("dataset", "value"),
    [
        ("entities", [{"entity_id": "light.one"}, {"entity_id": "light.one"}]),
        ("devices", [{"id": "device-1"}, {"id": "device-1"}]),
        ("areas", [{"id": "area-1"}, {"id": "area-1"}]),
        ("integrations", [{"entry_id": "entry-1"}, {"entry_id": "entry-1"}]),
        ("states", [{"entity_id": "light.one"}, {"entity_id": "light.one"}]),
        ("services", [{"domain": "light"}, {"domain": "light"}]),
    ],
)
def test_rejects_duplicate_runtime_identities(dataset: str, value: object) -> None:
    inventory = _inventory()
    homeassistant = dict(inventory.homeassistant)
    homeassistant[dataset] = value

    with pytest.raises(RuntimeTopologyError, match="invalid"):
        build_runtime_topology(RuntimeInventoryInput(manifest={}, homeassistant=homeassistant))


def test_rejects_malformed_relationships_instead_of_inventing_nodes() -> None:
    inventory = _inventory()
    homeassistant = dict(inventory.homeassistant)
    homeassistant["devices"] = [{"id": "device-1", "config_entries": "entry-1"}]

    with pytest.raises(RuntimeTopologyError, match="invalid"):
        build_runtime_topology(RuntimeInventoryInput(manifest={}, homeassistant=homeassistant))
