from __future__ import annotations

import math

import pytest
from ha_syncapp.retrigger_scheduler import (
    RetriggerScheduler,
    RetriggerSchedulerError,
)


def test_scheduler_first_deadline_and_regular_cadence() -> None:
    scheduler = RetriggerScheduler(interval_seconds=300)
    scheduler.arm(10.0)

    assert scheduler.due(309.999) is False
    assert scheduler.due(310.0) is True
    assert scheduler.due(609.999) is False
    assert scheduler.due(610.0) is True


def test_scheduler_coalesces_missed_intervals_into_one_cycle() -> None:
    scheduler = RetriggerScheduler(interval_seconds=60)
    scheduler.arm(100.0)

    assert scheduler.due(1000.0) is True
    assert scheduler.due(1000.0) is False
    assert scheduler.due(1059.999) is False
    assert scheduler.due(1060.0) is True


def test_scheduler_disarm_prevents_future_cycles() -> None:
    scheduler = RetriggerScheduler(interval_seconds=30)
    scheduler.arm(1.0)
    scheduler.disarm()

    assert scheduler.due(1000.0) is False
    assert scheduler.armed is False


def test_scheduler_can_rearm_after_disarm() -> None:
    scheduler = RetriggerScheduler(interval_seconds=30)
    scheduler.arm(1.0)
    scheduler.disarm()
    scheduler.arm(50.0)

    assert scheduler.due(79.999) is False
    assert scheduler.due(80.0) is True


@pytest.mark.parametrize("interval", [29, 3601, 0, -1, True, 30.5])
def test_scheduler_rejects_invalid_interval(interval: object) -> None:
    with pytest.raises(RetriggerSchedulerError, match="interval"):
        RetriggerScheduler(interval_seconds=interval)  # type: ignore[arg-type]


@pytest.mark.parametrize("now", [-1.0, math.inf, -math.inf, math.nan])
def test_scheduler_rejects_invalid_monotonic_evidence(now: float) -> None:
    scheduler = RetriggerScheduler(interval_seconds=30)
    with pytest.raises(RetriggerSchedulerError, match="monotonic"):
        scheduler.arm(now)


def test_scheduler_rejects_backward_monotonic_time() -> None:
    scheduler = RetriggerScheduler(interval_seconds=30)
    scheduler.arm(100.0)
    assert scheduler.due(110.0) is False

    with pytest.raises(RetriggerSchedulerError, match="backward"):
        scheduler.due(109.0)
