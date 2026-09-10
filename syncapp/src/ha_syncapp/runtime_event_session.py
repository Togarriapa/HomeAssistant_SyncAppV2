"""Runtime event-session callbacks that schedule durable runtime work only."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .runtime_event_trigger import RuntimeEventTriggerError, schedule_runtime_for_event
from .runtime_sync_schedule import RuntimeSyncScheduleError, schedule_runtime_sync_generation
from .state import StateStore


class RuntimeEventSessionError(RuntimeError):
    """Runtime event-session scheduling failed closed."""


class RuntimeEventSession:
    """Bind verified stream readiness and normalized events to routine work."""

    def __init__(self, store: StateStore, target: str) -> None:
        if type(store) is not StateStore:
            raise RuntimeEventSessionError("runtime event session state store is invalid")
        self._store = store
        self._target = target

    async def ready(self, *, now: datetime | None = None) -> None:
        """Schedule a full runtime baseline after every subscription is confirmed."""
        try:
            schedule_runtime_sync_generation(self._store, self._target, now=now)
        except RuntimeSyncScheduleError as exc:
            raise RuntimeEventSessionError("runtime event baseline scheduling failed closed") from exc

    async def event(
        self,
        event: Mapping[str, object],
        *,
        now: datetime | None = None,
    ) -> None:
        """Schedule runtime work from one normalized event without executing it."""
        try:
            schedule_runtime_for_event(self._store, self._target, event, now=now)
        except RuntimeEventTriggerError as exc:
            raise RuntimeEventSessionError("runtime event scheduling failed closed") from exc
