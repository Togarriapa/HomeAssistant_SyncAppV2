"""Contract tests for the recurring Retrigger recovery scheduler."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from ha_syncapp.retrigger_schedule import RetriggerSchedule, RetriggerScheduleError


def test_scheduler_waits_for_full_interval_before_first_cycle() -> None:
    run_cycle = Mock(return_value="completed")
    scheduler = RetriggerSchedule(interval_seconds=300, run_cycle=run_cycle)

    scheduler.start(100.0)

    assert scheduler.tick(399.999) is None
    run_cycle.assert_not_called()
    assert scheduler.tick(400.0) == "completed"
    run_cycle.assert_called_once_with()


def test_scheduler_coalesces_missed_intervals_into_one_cycle() -> None:
    run_cycle = Mock(return_value="completed")
    scheduler = RetriggerSchedule(interval_seconds=60, run_cycle=run_cycle)
    scheduler.start(10.0)

    assert scheduler.tick(250.0) == "completed"
    run_cycle.assert_called_once_with()
    assert scheduler.tick(250.0) is None


def test_scheduler_never_rearms_a_cycle_from_its_outcome() -> None:
    run_cycle = Mock(return_value="cycle_failed")
    scheduler = RetriggerSchedule(interval_seconds=60, run_cycle=run_cycle)
    scheduler.start(10.0)

    assert scheduler.tick(70.0) == "cycle_failed"
    assert scheduler.tick(129.999) is None
    assert scheduler.tick(130.0) == "cycle_failed"
    assert run_cycle.call_count == 2


def test_scheduler_stops_cleanly() -> None:
    run_cycle = Mock(return_value="completed")
    scheduler = RetriggerSchedule(interval_seconds=60, run_cycle=run_cycle)
    scheduler.start(10.0)
    scheduler.stop()

    assert scheduler.tick(1000.0) is None
    run_cycle.assert_not_called()


@pytest.mark.parametrize("interval", [True, False, 0, -1, 29, 3601, 1.5, "60"])
def test_scheduler_rejects_invalid_intervals(interval: object) -> None:
    with pytest.raises(RetriggerScheduleError, match="interval"):
        RetriggerSchedule(interval_seconds=interval, run_cycle=lambda: "completed")  # type: ignore[arg-type]


def test_scheduler_rejects_backward_monotonic_time() -> None:
    scheduler = RetriggerSchedule(interval_seconds=60, run_cycle=lambda: "completed")
    scheduler.start(100.0)

    with pytest.raises(RetriggerScheduleError, match="monotonic"):
        scheduler.tick(99.0)


def test_scheduler_requires_start_before_tick() -> None:
    scheduler = RetriggerSchedule(interval_seconds=60, run_cycle=lambda: "completed")

    with pytest.raises(RetriggerScheduleError, match="started"):
        scheduler.tick(100.0)
