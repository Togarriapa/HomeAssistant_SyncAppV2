"""Pure Home Assistant event classification for routine runtime publication."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Final

from .runtime_sync_schedule import RuntimeSyncScheduleError, schedule_runtime_sync_generation
from .state import StateStore, WorkItem

_MAX_EVENT_TYPE_LENGTH: Final = 128
_RELEVANT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "area_registry_updated",
        "category_registry_updated",
        "component_loaded",
        "core_config_updated",
        "device_registry_updated",
        "entity_registry_updated",
        "floor_registry_updated",
        "label_registry_updated",
        "service_registered",
        "service_removed",
        "state_changed",
    }
)


class RuntimeEventTriggerError(RuntimeError):
    """Runtime event evidence could not be classified safely."""


def schedule_runtime_for_event(
    store: StateStore,
    target: str,
    event: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> WorkItem | None:
    """Schedule runtime work for one normalized relevant Home Assistant event."""
    event_type = _event_type(event)
    if event_type not in _RELEVANT_EVENT_TYPES:
        return None
    try:
        return schedule_runtime_sync_generation(store, target, now=now)
    except RuntimeSyncScheduleError as exc:
        raise RuntimeEventTriggerError("runtime event scheduling failed closed") from exc


def _event_type(event: Mapping[str, object]) -> str:
    if not isinstance(event, Mapping) or set(event) != {"event_type"}:
        raise RuntimeEventTriggerError("runtime event evidence is invalid")
    event_type = event.get("event_type")
    if (
        not isinstance(event_type, str)
        or not event_type
        or event_type != event_type.strip()
        or len(event_type) > _MAX_EVENT_TYPE_LENGTH
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in event_type)
    ):
        raise RuntimeEventTriggerError("runtime event evidence is invalid")
    return event_type
