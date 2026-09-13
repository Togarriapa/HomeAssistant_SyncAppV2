"""Cadence for the recurring Retrigger recovery mechanism."""

from __future__ import annotations

from collections.abc import Callable

_MIN_INTERVAL_SECONDS = 30
_MAX_INTERVAL_SECONDS = 3600


class RetriggerScheduleError(RuntimeError):
    """The recurring recovery schedule cannot be used safely."""


class RetriggerSchedule:
    """Run at most one bounded recovery dispatch when a monotonic deadline is due."""

    def __init__(self, *, interval_seconds: int, run_cycle: Callable[[], str]) -> None:
        if (
            type(interval_seconds) is not int
            or not _MIN_INTERVAL_SECONDS <= interval_seconds <= _MAX_INTERVAL_SECONDS
        ):
            raise RetriggerScheduleError("Retrigger interval is invalid")
        if not callable(run_cycle):
            raise RetriggerScheduleError("Retrigger cycle callback is invalid")
        self._interval_seconds = interval_seconds
        self._run_cycle = run_cycle
        self._next_due: float | None = None
        self._last_now: float | None = None
        self._started = False
        self._stopped = False

    def start(self, now: float) -> None:
        """Arm the first dispatch one full interval after scheduler activation."""
        now_value = self._validate_now(now)
        self._last_now = now_value
        self._next_due = now_value + self._interval_seconds
        self._started = True
        self._stopped = False

    def tick(self, now: float) -> str | None:
        """Execute one due dispatch and coalesce any missed intervals."""
        if self._stopped:
            return None
        if not self._started or self._next_due is None or self._last_now is None:
            raise RetriggerScheduleError("Retrigger schedule has not been started")
        now_value = self._validate_now(now)
        if now_value < self._last_now:
            raise RetriggerScheduleError("Retrigger monotonic time moved backwards")
        self._last_now = now_value
        if now_value < self._next_due:
            return None

        try:
            return self._run_cycle()
        finally:
            # A transport failure must not create a tight retry loop. Durable work owns
            # its own retry/backoff semantics once a request reaches the service.
            self._next_due = now_value + self._interval_seconds

    def stop(self) -> None:
        """Disarm automatic recovery without changing durable work state."""
        self._next_due = None
        self._last_now = None
        self._stopped = True

    @staticmethod
    def _validate_now(now: float) -> float:
        if not isinstance(now, (int, float)) or isinstance(now, bool):
            raise RetriggerScheduleError("Retrigger monotonic time is invalid")
        now_value = float(now)
        if now_value < 0:
            raise RetriggerScheduleError("Retrigger monotonic time is invalid")
        return now_value
