from __future__ import annotations

import math
from dataclasses import dataclass

MIN_RETRIGGER_INTERVAL_SECONDS = 30
MAX_RETRIGGER_INTERVAL_SECONDS = 3600
DEFAULT_RETRIGGER_INTERVAL_SECONDS = 300


class RetriggerSchedulerError(ValueError):
    """Recurring Retrigger cadence evidence is invalid."""


@dataclass(slots=True)
class RetriggerScheduler:
    """Owner-loop monotonic cadence with missed-interval coalescing."""

    interval_seconds: int
    _armed: bool = False
    _next_due: float | None = None
    _last_now: float | None = None

    def __post_init__(self) -> None:
        if (
            type(self.interval_seconds) is not int
            or self.interval_seconds < MIN_RETRIGGER_INTERVAL_SECONDS
            or self.interval_seconds > MAX_RETRIGGER_INTERVAL_SECONDS
        ):
            raise RetriggerSchedulerError("Retrigger interval is outside safety bounds")

    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self, now: float) -> None:
        self._validate_now(now)
        self._armed = True
        self._last_now = now
        self._next_due = now + self.interval_seconds

    def disarm(self) -> None:
        self._armed = False
        self._next_due = None
        self._last_now = None

    def due(self, now: float) -> bool:
        self._validate_now(now)
        if not self._armed:
            return False
        if self._last_now is not None and now < self._last_now:
            raise RetriggerSchedulerError("monotonic time moved backward")
        self._last_now = now
        next_due = self._next_due
        if next_due is None or now < next_due:
            return False
        self._next_due = now + self.interval_seconds
        return True

    @staticmethod
    def _validate_now(now: float) -> None:
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise RetriggerSchedulerError("monotonic time evidence is invalid")
        value = float(now)
        if not math.isfinite(value) or value < 0:
            raise RetriggerSchedulerError("monotonic time evidence is invalid")
