"""Deterministic dependency/topology evidence from read-only Home Assistant runtime data."""

from __future__ import annotations

from collections.abc import Mapping

from .runtime_inventory import RuntimeInventoryInput

_AUTHORITY = {
    "derived_relationships": "best_effort",
    "registry_relationships": "authoritative",
    "runtime_state": "observational",
}


class RuntimeTopologyError(RuntimeError):
    """Runtime evidence is too ambiguous or malformed to build trusted topology."""


def build_runtime_topology(inventory: RuntimeInventoryInput) -> RuntimeInventoryInput:
    """Build deterministic read-only topology without mutating the collected inventory."""
    if type(inventory) is not RuntimeInventoryInput:
        raise RuntimeTopologyError("runtime topology input is invalid")

    homeassistant = inventory.homeassistant
    entities = _index(homeassistant, "entities", "entity_id")
    devices = _index(homeassistant, "devices", "id")
    integrations = _index(homeassistant, "integrations", "entry_id")
    areas = _index(homeassistant, "areas", "id")
    floors = _index(homeassistant, "floors", "id")
    labels = _index(homeassistant, "labels", "id")
    states = _index(homeassistant, "states", "entity_id")
    services = _index(homeassistant, "services", "domain")

    edges: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    entity_dependencies: list[dict[str, object]] = []

    for entity_id, entity in entities.items():
        device_id = _optional_id(entity, "device_id")
        config_entry_id = _optional_id(entity, "config_entry_id")
        direct_area_id = _optional_id(entity, "area_id")
        entity_labels = _id_list(entity, "labels")

        _relation(
            edges,
            unresolved,
            source_type="entity",
            source_id=entity_id,
            relation="belongs_to_device",
            target_type="device",
            target_id=device_id,
            targets=devices,
            authority="registry",
        )
        _relation(
            edges,
            unresolved,
            source_type="entity",
            source_id=entity_id,
            relation="configured_by",
            target_type="integration",
            target_id=config_entry_id,
            targets=integrations,
            authority="registry",
        )

        area_id = direct_area_id
        area_source: str | None = "registry" if direct_area_id is not None else None
        if direct_area_id is not None:
            _relation(
                edges,
                unresolved,
                source_type="entity",
                source_id=entity_id,
                relation="located_in",
                target_type="area",
                target_id=direct_area_id,
                targets=areas,
                authority="registry",
            )
        elif device_id is not None and device_id in devices:
            inherited_area = _optional_id(devices[device_id], "area_id")
            if inherited_area is not None:
                area_id = inherited_area
                area_source = "derived_via_device"
                _relation(
                    edges,
                    unresolved,
                    source_type="entity",
                    source_id=entity_id,
                    relation="located_in",
                    target_type="area",
                    target_id=inherited_area,
                    targets=areas,
                    authority="derived",
                )

        for label_id in entity_labels:
            _relation(
                edges,
                unresolved,
                source_type="entity",
                source_id=entity_id,
                relation="has_label",
                target_type="label",
                target_id=label_id,
                targets=labels,
                authority="registry",
            )

        state_record = states.get(entity_id)
        state = state_record.get("state") if state_record is not None else None
        if state is not None and not isinstance(state, str):
            raise RuntimeTopologyError("runtime topology state evidence is invalid")
        entity_dependencies.append(
            {
                "area_id": area_id,
                "area_source": area_source,
                "available": state not in {None, "unavailable", "unknown"},
                "config_entry_id": config_entry_id,
                "device_id": device_id,
                "entity_id": entity_id,
                "labels": entity_labels,
                "state": state,
            }
        )

    for device_id, device in devices.items():
        for entry_id in _id_list(device, "config_entries"):
            _relation(
                edges,
                unresolved,
                source_type="device",
                source_id=device_id,
                relation="configured_by",
                target_type="integration",
                target_id=entry_id,
                targets=integrations,
                authority="registry",
            )
        _relation(
            edges,
            unresolved,
            source_type="device",
            source_id=device_id,
            relation="located_in",
            target_type="area",
            target_id=_optional_id(device, "area_id"),
            targets=areas,
            authority="registry",
        )
        for label_id in _id_list(device, "labels"):
            _relation(
                edges,
                unresolved,
                source_type="device",
                source_id=device_id,
                relation="has_label",
                target_type="label",
                target_id=label_id,
                targets=labels,
                authority="registry",
            )

    for area_id, area in areas.items():
        _relation(
            edges,
            unresolved,
            source_type="area",
            source_id=area_id,
            relation="on_floor",
            target_type="floor",
            target_id=_optional_id(area, "floor_id"),
            targets=floors,
            authority="registry",
        )
        for label_id in _id_list(area, "labels"):
            _relation(
                edges,
                unresolved,
                source_type="area",
                source_id=area_id,
                relation="has_label",
                target_type="label",
                target_id=label_id,
                targets=labels,
                authority="registry",
            )

    edges.sort(key=_edge_key)
    unresolved.sort(key=_edge_key)
    entity_dependencies.sort(key=lambda item: str(item["entity_id"]))
    service_domains = sorted(services)

    topology = {
        "schema_version": 1,
        "authority": dict(_AUTHORITY),
        "edges": edges,
        "nodes": {
            "areas": sorted(areas),
            "devices": sorted(devices),
            "entities": sorted(entities),
            "floors": sorted(floors),
            "integrations": sorted(integrations),
            "labels": sorted(labels),
        },
        "unresolved": unresolved,
    }
    dependencies = {
        "schema_version": 1,
        "authority": dict(_AUTHORITY),
        "entities": entity_dependencies,
        "service_domains": service_domains,
        "unresolved": unresolved,
    }
    return RuntimeInventoryInput(
        manifest={
            "topology_entity_count": len(entities),
            "topology_device_count": len(devices),
            "topology_unresolved_count": len(unresolved),
        },
        analysis={"topology": topology, "dependencies": dependencies},
    )


def _index(
    homeassistant: Mapping[str, object],
    dataset: str,
    identity_key: str,
) -> dict[str, dict[str, object]]:
    value = homeassistant.get(dataset, [])
    if not isinstance(value, list):
        raise RuntimeTopologyError("runtime topology dataset is invalid")
    indexed: dict[str, dict[str, object]] = {}
    for item in value:
        if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
            raise RuntimeTopologyError("runtime topology dataset is invalid")
        identity = item.get(identity_key)
        if not isinstance(identity, str) or not identity or identity in indexed:
            raise RuntimeTopologyError("runtime topology dataset identity is invalid")
        indexed[identity] = dict(item)
    return dict(sorted(indexed.items()))


def _optional_id(item: Mapping[str, object], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuntimeTopologyError("runtime topology relationship is invalid")
    return value


def _id_list(item: Mapping[str, object], key: str) -> list[str]:
    value = item.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeTopologyError("runtime topology relationship is invalid")
    identities: set[str] = set()
    for entry in value:
        if not isinstance(entry, str) or not entry or entry in identities:
            raise RuntimeTopologyError("runtime topology relationship is invalid")
        identities.add(entry)
    return sorted(identities)


def _relation(
    edges: list[dict[str, str]],
    unresolved: list[dict[str, str]],
    *,
    source_type: str,
    source_id: str,
    relation: str,
    target_type: str,
    target_id: str | None,
    targets: Mapping[str, object],
    authority: str,
) -> None:
    if target_id is None:
        return
    record = {
        "source_id": source_id,
        "source_type": source_type,
        "relation": relation,
        "target_id": target_id,
        "target_type": target_type,
    }
    if target_id not in targets:
        unresolved.append(record)
        return
    edges.append({**record, "authority": authority})


def _edge_key(item: Mapping[str, str]) -> tuple[str, str, str, str, str]:
    return (
        item["source_type"],
        item["source_id"],
        item["relation"],
        item["target_type"],
        item["target_id"],
    )
