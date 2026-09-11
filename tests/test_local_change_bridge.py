from pathlib import Path

import pytest
from ha_syncapp.local_change_bridge import LocalChangeBridge, LocalChangeBridgeError
from ha_syncapp.local_change_debounce import LocalChangeDebouncer
from ha_syncapp.local_change_source import LocalChangeEntry, LocalChangeSnapshot
from ha_syncapp.local_sync_work import local_sync_work_key
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "homeassistant"
    source.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    return source


def test_meaningful_event_waits_for_quiet_period_then_schedules(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(tmp_path)
    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=5.0)
        bridge = LocalChangeBridge(source, debounce)

        (source / "automations.yaml").write_text("[]\n")

        assert bridge.notify_source_event(10.0) is True
        assert bridge.tick(14.999) is None
        scheduled = bridge.tick(15.0)

        assert scheduled is not None
        assert scheduled.work_kind == "local_sync"
        assert scheduled.work_key == local_sync_work_key(TARGET, "main")
        assert scheduled.status == "pending"
    finally:
        store.__exit__(None, None, None)


def test_ignored_source_churn_does_not_arm_debounce(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(tmp_path)
    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=1.0)
        bridge = LocalChangeBridge(source, debounce)

        (source / "home-assistant_v2.db").write_bytes(b"recorder churn")
        (source / "home-assistant.log").write_text("log churn\n")

        assert bridge.notify_source_event(10.0) is False
        assert bridge.tick(100.0) is None
        assert store.get_work(local_sync_work_key(TARGET, "main")) is None
    finally:
        store.__exit__(None, None, None)


def test_repeated_meaningful_events_extend_existing_debounce(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(tmp_path)
    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=5.0)
        bridge = LocalChangeBridge(source, debounce)

        (source / "automations.yaml").write_text("[]\n")
        assert bridge.notify_source_event(10.0) is True
        (source / "scripts.yaml").write_text("{}\n")
        assert bridge.notify_source_event(13.0) is True

        assert bridge.tick(15.0) is None
        assert bridge.tick(17.999) is None
        assert bridge.tick(18.0) is not None
    finally:
        store.__exit__(None, None, None)


def test_observation_failure_is_fail_closed_without_losing_baseline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(tmp_path)
    baseline = LocalChangeSnapshot(1, 2, ())
    changed = LocalChangeSnapshot(
        1,
        2,
        (
            LocalChangeEntry(
                path="configuration.yaml",
                device=1,
                inode=3,
                mode=0o100600,
                links=1,
                size=1,
                mtime_ns=1,
                ctime_ns=1,
            ),
        ),
    )
    observations: list[object] = [baseline, OSError("unsafe"), changed]

    def observer(_: Path) -> LocalChangeSnapshot:
        result = observations.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, LocalChangeSnapshot)
        return result

    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=1.0)
        bridge = LocalChangeBridge(source, debounce, observer=observer)

        with pytest.raises(LocalChangeBridgeError, match="failed closed"):
            bridge.notify_source_event(10.0)

        assert bridge.notify_source_event(11.0) is True
        assert bridge.tick(12.0) is not None
    finally:
        store.__exit__(None, None, None)


def test_initial_observation_failure_prevents_activation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(tmp_path)

    def observer(_: Path) -> LocalChangeSnapshot:
        raise OSError("unavailable")

    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=1.0)
        with pytest.raises(LocalChangeBridgeError, match="failed closed"):
            LocalChangeBridge(source, debounce, observer=observer)
    finally:
        store.__exit__(None, None, None)
