from pathlib import Path

import pytest
from ha_syncapp.local_change_debounce import LocalChangeDebounceError, LocalChangeDebouncer
from ha_syncapp.local_sync_work import local_sync_work_key
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_first_signal_waits_for_complete_quiet_period(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=5.0)
        debounce.notify(10.0)

        assert debounce.tick(14.999) is None
        scheduled = debounce.tick(15.0)

        assert scheduled is not None
        assert scheduled.work_kind == "local_sync"
        assert scheduled.work_key == local_sync_work_key(TARGET, "main")
        assert scheduled.status == "pending"
    finally:
        store.__exit__(None, None, None)


def test_repeated_signals_extend_quiet_period(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=5.0)
        debounce.notify(10.0)
        debounce.notify(13.0)

        assert debounce.tick(15.0) is None
        assert debounce.tick(17.999) is None
        assert debounce.tick(18.0) is not None
    finally:
        store.__exit__(None, None, None)


def test_stable_signal_schedules_only_once_until_next_notification(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=2.0)
        debounce.notify(1.0)

        first = debounce.tick(3.0)
        assert first is not None
        assert debounce.tick(4.0) is None
        assert debounce.tick(100.0) is None
    finally:
        store.__exit__(None, None, None)


def test_new_signal_after_success_can_start_fresh_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=1.0)
        debounce.notify(1.0)
        first = debounce.tick(2.0)
        assert first is not None
        claimed = store.claim_work_kind("local_sync")
        assert claimed is not None
        completed = store.complete_work(claimed)
        assert completed.status == "succeeded"

        debounce.notify(3.0)
        second = debounce.tick(4.0)

        assert second is not None
        assert second.status == "pending"
        assert second.attempts == 0
    finally:
        store.__exit__(None, None, None)


def test_blocked_generation_is_not_rearmed_by_change_signal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        key = local_sync_work_key(TARGET, "main")
        store.enqueue_work("local_sync", key)
        claimed = store.claim_work_kind("local_sync")
        assert claimed is not None
        blocked = store.fail_work(claimed, transient=False)
        assert blocked.status == "blocked"

        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=1.0)
        debounce.notify(5.0)
        result = debounce.tick(6.0)

        assert result is not None
        assert result.status == "blocked"
    finally:
        store.__exit__(None, None, None)


def test_non_monotonic_time_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=5.0)
        debounce.notify(10.0)

        with pytest.raises(LocalChangeDebounceError, match="monotonic"):
            debounce.notify(9.0)
        with pytest.raises(LocalChangeDebounceError, match="monotonic"):
            debounce.tick(9.5)
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan"), True])
def test_invalid_time_values_fail_closed(tmp_path: Path, value: object) -> None:
    store = _store(tmp_path)
    try:
        debounce = LocalChangeDebouncer(store, TARGET, quiet_seconds=5.0)
        with pytest.raises(LocalChangeDebounceError, match="time"):
            debounce.notify(value)  # type: ignore[arg-type]
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("quiet", [0.0, -1.0, float("inf"), float("nan"), True])
def test_invalid_quiet_period_fails_closed(tmp_path: Path, quiet: object) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(LocalChangeDebounceError, match="quiet period"):
            LocalChangeDebouncer(store, TARGET, quiet_seconds=quiet)  # type: ignore[arg-type]
    finally:
        store.__exit__(None, None, None)
