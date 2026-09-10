"""Bounded cross-context mailbox for normalized Home Assistant runtime signals."""

from __future__ import annotations

import queue
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .runtime_event_trigger import (
    RuntimeEventTriggerError,
    runtime_event_types,
    schedule_runtime_for_event,
)
from .runtime_sync_schedule import RuntimeSyncScheduleError, schedule_runtime_sync_generation
from .state import StateStore

_DEFAULT_CAPACITY: Final = 256
_MAX_CAPACITY: Final = 4096
_DEFAULT_DRAIN_LIMIT: Final = 128
_MAX_DRAIN_LIMIT: Final = 1024
_ALLOWED_EVENT_TYPES: Final = frozenset(runtime_event_types())


class RuntimeEventMailboxError(RuntimeError):
    """Runtime event mailbox evidence could not be handled safely."""


class RuntimeEventSignalKind(StrEnum):
    """The only signal classes allowed across the runtime transport boundary."""

    READY = "ready"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class RuntimeEventSignal:
    """One bounded normalized signal; raw Home Assistant payloads are forbidden."""

    kind: RuntimeEventSignalKind
    event_type: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeEventMailboxDrainResult:
    """Sanitized outcome from one bounded owner-thread mailbox drain."""

    consumed: int
    ready_signals: int
    event_signals: int


class RuntimeEventMailbox:
    """Thread-safe bounded mailbox whose callbacks retain only normalized evidence."""

    def __init__(self, *, capacity: int = _DEFAULT_CAPACITY) -> None:
        if type(capacity) is not int or not 1 <= capacity <= _MAX_CAPACITY:
            raise RuntimeEventMailboxError("runtime event mailbox capacity is invalid")
        self._queue: queue.Queue[RuntimeEventSignal] = queue.Queue(maxsize=capacity)

    async def ready(self) -> None:
        """Enqueue one subscription-readiness signal without blocking a transport thread."""
        self._enqueue(RuntimeEventSignal(RuntimeEventSignalKind.READY))

    async def event(self, evidence: Mapping[str, object]) -> None:
        """Reduce already-normalized event evidence to one bounded event-type signal."""
        if not isinstance(evidence, Mapping) or set(evidence) != {"event_type"}:
            raise RuntimeEventMailboxError("runtime event mailbox evidence is invalid")
        event_type = evidence.get("event_type")
        if not isinstance(event_type, str) or event_type not in _ALLOWED_EVENT_TYPES:
            raise RuntimeEventMailboxError("runtime event mailbox evidence is invalid")
        self._enqueue(RuntimeEventSignal(RuntimeEventSignalKind.EVENT, event_type))

    def _enqueue(self, signal: RuntimeEventSignal) -> None:
        _validate_signal(signal)
        try:
            self._queue.put_nowait(signal)
        except queue.Full:
            raise RuntimeEventMailboxError("runtime event mailbox capacity exhausted") from None

    def _next(self) -> RuntimeEventSignal | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


def drain_runtime_event_mailbox(
    mailbox: RuntimeEventMailbox,
    store: StateStore,
    target: str,
    *,
    max_signals: int = _DEFAULT_DRAIN_LIMIT,
) -> RuntimeEventMailboxDrainResult:
    """Schedule at most ``max_signals`` on the StateStore-owning thread."""
    if type(mailbox) is not RuntimeEventMailbox:
        raise RuntimeEventMailboxError("runtime event mailbox is invalid")
    if type(store) is not StateStore:
        raise RuntimeEventMailboxError("runtime event mailbox state store is invalid")
    if type(max_signals) is not int or not 1 <= max_signals <= _MAX_DRAIN_LIMIT:
        raise RuntimeEventMailboxError("runtime event mailbox drain limit is invalid")

    consumed = 0
    ready_signals = 0
    event_signals = 0
    try:
        while consumed < max_signals:
            signal = mailbox._next()
            if signal is None:
                break
            _validate_signal(signal)
            if signal.kind is RuntimeEventSignalKind.READY:
                schedule_runtime_sync_generation(store, target)
                ready_signals += 1
            else:
                schedule_runtime_for_event(
                    store,
                    target,
                    {"event_type": signal.event_type},
                )
                event_signals += 1
            consumed += 1
    except (RuntimeSyncScheduleError, RuntimeEventTriggerError) as exc:
        raise RuntimeEventMailboxError("runtime event mailbox drain failed closed") from exc

    return RuntimeEventMailboxDrainResult(
        consumed=consumed,
        ready_signals=ready_signals,
        event_signals=event_signals,
    )


def _validate_signal(signal: RuntimeEventSignal) -> None:
    if type(signal) is not RuntimeEventSignal or type(signal.kind) is not RuntimeEventSignalKind:
        raise RuntimeEventMailboxError("runtime event mailbox signal is invalid")
    if signal.kind is RuntimeEventSignalKind.READY:
        if signal.event_type is not None:
            raise RuntimeEventMailboxError("runtime event mailbox signal is invalid")
        return
    if not isinstance(signal.event_type, str) or signal.event_type not in _ALLOWED_EVENT_TYPES:
        raise RuntimeEventMailboxError("runtime event mailbox signal is invalid")
