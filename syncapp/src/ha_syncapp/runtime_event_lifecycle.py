"""Bounded reconnect lifecycle for read-only Home Assistant runtime events."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .core_event_stream import CoreEventStreamError, consume_core_runtime_events
from .runtime_event_session import RuntimeEventSession
from .state import StateStore

_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
_DEFAULT_MAX_BACKOFF_SECONDS = 30.0
_MAX_ATTEMPTS = 20
_MAX_BACKOFF_SECONDS = 300.0


class RuntimeEventLifecycleError(RuntimeError):
    """The bounded runtime event lifecycle failed closed."""


class RuntimeEventConsumer(Protocol):
    """Minimum subscriber surface required by the reconnect lifecycle."""

    async def __call__(
        self,
        handler: Callable[[Mapping[str, object]], Awaitable[None]],
        *,
        on_ready: Callable[[], Awaitable[None]] | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class RuntimeEventLifecycleResult:
    """Sanitized outcome from one bounded lifecycle invocation."""

    attempts: int
    reconnects: int
    stopped: bool
    events_consumed: int


async def run_runtime_event_lifecycle(
    store: StateStore,
    target: str,
    *,
    token: str | None = None,
    stop_event: asyncio.Event | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    initial_backoff_seconds: float = _DEFAULT_INITIAL_BACKOFF_SECONDS,
    max_backoff_seconds: float = _DEFAULT_MAX_BACKOFF_SECONDS,
    consumer: RuntimeEventConsumer = consume_core_runtime_events,
) -> RuntimeEventLifecycleResult:
    """Consume runtime events with bounded reconnect attempts and interruptible backoff."""
    _validate_inputs(
        store,
        target,
        stop_event,
        max_attempts,
        initial_backoff_seconds,
        max_backoff_seconds,
        consumer,
    )
    stop = stop_event or asyncio.Event()
    attempts = 0
    reconnects = 0
    total_events = 0

    while attempts < max_attempts and not stop.is_set():
        attempts += 1
        session = RuntimeEventSession(store, target)
        try:
            consumed = await consumer(
                session.event,
                on_ready=session.ready,
                token=token,
                max_events=None,
            )
        except CoreEventStreamError:
            if stop.is_set():
                return RuntimeEventLifecycleResult(attempts, reconnects, True, total_events)
            if attempts >= max_attempts:
                raise RuntimeEventLifecycleError(
                    "runtime event reconnect attempts exhausted"
                ) from None
            reconnects += 1
            delay = min(
                initial_backoff_seconds * (2 ** (attempts - 1)),
                max_backoff_seconds,
            )
            if await _wait_for_stop(stop, delay):
                return RuntimeEventLifecycleResult(attempts, reconnects, True, total_events)
            continue
        except Exception:
            raise RuntimeEventLifecycleError("runtime event lifecycle failed closed") from None

        if type(consumed) is not int or consumed < 0:
            raise RuntimeEventLifecycleError("runtime event lifecycle result is invalid")
        total_events += consumed
        return RuntimeEventLifecycleResult(attempts, reconnects, stop.is_set(), total_events)

    return RuntimeEventLifecycleResult(attempts, reconnects, True, total_events)


async def _wait_for_stop(stop_event: asyncio.Event, delay_seconds: float) -> bool:
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
    except TimeoutError:
        return False
    return True


def _validate_inputs(
    store: StateStore,
    target: str,
    stop_event: asyncio.Event | None,
    max_attempts: int,
    initial_backoff_seconds: float,
    max_backoff_seconds: float,
    consumer: RuntimeEventConsumer,
) -> None:
    if type(store) is not StateStore:
        raise RuntimeEventLifecycleError("runtime event lifecycle state store is invalid")
    if not isinstance(target, str) or not target or target != target.strip():
        raise RuntimeEventLifecycleError("runtime event lifecycle target is invalid")
    if stop_event is not None and not isinstance(stop_event, asyncio.Event):
        raise RuntimeEventLifecycleError("runtime event lifecycle stop signal is invalid")
    if type(max_attempts) is not int or not 1 <= max_attempts <= _MAX_ATTEMPTS:
        raise RuntimeEventLifecycleError("runtime event lifecycle attempt limit is invalid")
    for value in (initial_backoff_seconds, max_backoff_seconds):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise RuntimeEventLifecycleError("runtime event lifecycle backoff is invalid")
        if value <= 0 or value > _MAX_BACKOFF_SECONDS:
            raise RuntimeEventLifecycleError("runtime event lifecycle backoff is invalid")
    if initial_backoff_seconds > max_backoff_seconds:
        raise RuntimeEventLifecycleError("runtime event lifecycle backoff is invalid")
    if not callable(consumer):
        raise RuntimeEventLifecycleError("runtime event lifecycle consumer is invalid")
