from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.runtime_event_trigger import RuntimeEventTriggerError, schedule_runtime_for_event
from ha_syncapp.runtime_sync_work import runtime_sync_work_key
from ha_syncapp.state import StateStore


@pytest.mark.parametrize(
    "event_type",
    [
        "state_changed",
        "entity_registry_updated",
        "device_registry_updated",
        "area_registry_updated",
        "floor_registry_updated",
        "label_registry_updated",
        "category_registry_updated",
        "component_loaded",
        "core_config_updated",
        "service_registered",
        "service_removed",
    ],
)
def test_relevant_event_schedules_runtime_generation(tmp_path: Path, event_type: str) -> None:
    scheduled_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    with StateStore(tmp_path) as store:
        item = schedule_runtime_for_event(
            store,
            "Owner/Home",
            {"event_type": event_type},
            now=scheduled_at,
        )

    assert item is not None
    assert item.work_kind == "runtime"
    assert item.work_key == runtime_sync_work_key("Owner/Home")
    assert item.status == "pending"


def test_unrelated_event_creates_no_work(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        item = schedule_runtime_for_event(
            store,
            "Owner/Home",
            {"event_type": "call_service"},
        )
        assert item is None
        assert store.claim_work_kind("runtime") is None


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"event_type": "state_changed", "data": {}},
        {"event_type": ""},
        {"event_type": " state_changed"},
        {"event_type": "state_changed\n"},
        {"event_type": "x" * 129},
        {"event_type": 1},
    ],
)
def test_malformed_event_fails_closed(tmp_path: Path, event: dict[str, object]) -> None:
    with StateStore(tmp_path) as store, pytest.raises(RuntimeEventTriggerError):
        schedule_runtime_for_event(store, "Owner/Home", event)


def test_repeated_event_coalesces_active_generation(tmp_path: Path) -> None:
    first_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=1)
    with StateStore(tmp_path) as store:
        first = schedule_runtime_for_event(
            store,
            "Owner/Home",
            {"event_type": "state_changed"},
            now=first_at,
        )
        second = schedule_runtime_for_event(
            store,
            "Owner/Home",
            {"event_type": "entity_registry_updated"},
            now=second_at,
        )

    assert first is not None
    assert second == first


def test_event_does_not_rearm_blocked_runtime_work(tmp_path: Path) -> None:
    first_at = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=1)
    with StateStore(tmp_path) as store:
        scheduled = schedule_runtime_for_event(
            store,
            "Owner/Home",
            {"event_type": "state_changed"},
            now=first_at,
        )
        assert scheduled is not None
        running = store.claim_work_kind("runtime", now=first_at)
        assert running is not None
        blocked = store.fail_work(running, transient=False, now=first_at)
        repeated = schedule_runtime_for_event(
            store,
            "Owner/Home",
            {"event_type": "device_registry_updated"},
            now=second_at,
        )

    assert repeated == blocked
    assert repeated.status == "blocked"


def test_invalid_target_is_sanitized(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store, pytest.raises(RuntimeEventTriggerError) as error:
        schedule_runtime_for_event(
            store,
            " secret-sentinel ",
            {"event_type": "state_changed"},
        )
    assert "secret-sentinel" not in str(error.value)
