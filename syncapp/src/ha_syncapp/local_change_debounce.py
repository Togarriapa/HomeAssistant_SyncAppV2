"""Deterministic owner-thread debounce for routine Local synchronization signals."""

from __future__ import annotations

import math

from .local_sync_schedule import LocalSyncScheduleError, schedule_local_sync_generation
from .state import StateStore, WorkItem


class LocalChangeDebounceError(RuntimeError):
    """A local change signal could not be debounced safely."""


class LocalChangeDebouncer:
    """Coalesce normalized local change signals until a complete quiet period elapses."""

    def __init__(
        self,
        store: StateStore,
        target: str,
        *,
        quiet_seconds: float,
        branch: str = "main",
    ) -> None:
        if type(store) is not StateStore:
            raise LocalChangeDebounceError("local change state store is invalid")
        if not _valid_time_value(quiet_seconds) or quiet_seconds <= 0:
            raise LocalChangeDebounceError("local change quiet period is invalid")
        self._store = store
        self._target = target
        self._branch = branch
        self._quiet_seconds = float(quiet_seconds)
        self._last_time: float | None = None
        self._deadline: float | None = None

    def notify(self, now: float) -> None:
        """Record one normalized local-change signal and extend the quiet deadline."""
        current = self._advance_time(now)
        self._deadline = current + self._quiet_seconds

    def tick(self, now: float) -> WorkItem | None:
        """Schedule at most one routine Local generation once the source is quiet."""
        current = self._advance_time(now)
        if self._deadline is None or current < self._deadline:
            return None

        self._deadline = None
        try:
            return schedule_local_sync_generation(
                self._store,
                self._target,
                branch=self._branch,
            )
        except LocalSyncScheduleError as exc:
            raise LocalChangeDebounceError("local change scheduling failed closed") from exc

    def _advance_time(self, now: float) -> float:
        if not _valid_time_value(now):
            raise LocalChangeDebounceError("local change time is invalid")
        current = float(now)
        if self._last_time is not None and current < self._last_time:
            raise LocalChangeDebounceError("local change monotonic time moved backwards")
        self._last_time = current
        return current


def _valid_time_value(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value >= 0
