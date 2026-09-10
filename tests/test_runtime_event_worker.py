from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Mapping

from ha_syncapp.core_event_stream import CoreEventStreamError
from ha_syncapp.runtime_event_mailbox import RuntimeEventMailbox, RuntimeEventMailboxError
from ha_syncapp.runtime_event_worker import (
    RuntimeEventWorker,
    RuntimeEventWorkerReason,
)

EventHandler = Callable[[Mapping[str, object]], Awaitable[None]]
ReadyHandler = Callable[[], Awaitable[None]]


def test_worker_forwards_ready_and_normalized_event() -> None:
    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        assert token == "token"
        assert max_events is None
        assert on_ready is not None
        await on_ready()
        await handler({"event_type": "state_changed"})
        return 1

    worker = RuntimeEventWorker(RuntimeEventMailbox(), token="token", consumer=consumer)
    worker.start()
    result = worker.join(timeout_seconds=2)

    assert result.reason is RuntimeEventWorkerReason.COMPLETED
    assert result.attempts == 1
    assert result.reconnects == 0
    assert result.readiness_signals == 1
    assert result.events_forwarded == 1


def test_worker_reconnects_after_transient_core_failure() -> None:
    calls = 0

    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CoreEventStreamError("transient")
        return 0

    worker = RuntimeEventWorker(
        RuntimeEventMailbox(),
        max_attempts=2,
        initial_backoff_seconds=0.001,
        max_backoff_seconds=0.001,
        consumer=consumer,
    )
    worker.start()
    result = worker.join(timeout_seconds=2)

    assert result.reason is RuntimeEventWorkerReason.COMPLETED
    assert result.attempts == 2
    assert result.reconnects == 1
    assert calls == 2


def test_worker_exhausts_bounded_transient_retries() -> None:
    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        raise CoreEventStreamError("transient")

    worker = RuntimeEventWorker(
        RuntimeEventMailbox(),
        max_attempts=2,
        initial_backoff_seconds=0.001,
        max_backoff_seconds=0.001,
        consumer=consumer,
    )
    worker.start()
    result = worker.join(timeout_seconds=2)

    assert result.reason is RuntimeEventWorkerReason.EXHAUSTED
    assert result.attempts == 2
    assert result.reconnects == 1


def test_worker_fails_closed_when_mailbox_saturates_through_wrapped_core_error() -> None:
    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        assert on_ready is not None
        await on_ready()
        try:
            await on_ready()
        except RuntimeEventMailboxError:
            raise CoreEventStreamError("stream failed closed") from None
        return 0

    worker = RuntimeEventWorker(
        RuntimeEventMailbox(capacity=1),
        max_attempts=5,
        initial_backoff_seconds=0.001,
        max_backoff_seconds=0.001,
        consumer=consumer,
    )
    worker.start()
    result = worker.join(timeout_seconds=2)

    assert result.reason is RuntimeEventWorkerReason.FAILED
    assert result.attempts == 1
    assert result.reconnects == 0
    assert result.readiness_signals == 1


def test_stop_interrupts_active_event_wait() -> None:
    connected = threading.Event()

    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        connected.set()
        await asyncio.Event().wait()
        return 0

    worker = RuntimeEventWorker(RuntimeEventMailbox(), consumer=consumer)
    worker.start()
    assert connected.wait(timeout=1)
    worker.request_stop()
    result = worker.join(timeout_seconds=2)

    assert result.reason is RuntimeEventWorkerReason.STOPPED
    assert result.attempts == 1


def test_stop_interrupts_reconnect_backoff() -> None:
    failed = threading.Event()

    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        failed.set()
        raise CoreEventStreamError("transient")

    worker = RuntimeEventWorker(
        RuntimeEventMailbox(),
        max_attempts=5,
        initial_backoff_seconds=1,
        max_backoff_seconds=1,
        consumer=consumer,
    )
    worker.start()
    assert failed.wait(timeout=1)
    time.sleep(0.05)
    started = time.monotonic()
    worker.request_stop()
    result = worker.join(timeout_seconds=2)

    assert result.reason is RuntimeEventWorkerReason.STOPPED
    assert result.attempts == 1
    assert result.reconnects == 1
    assert time.monotonic() - started < 0.5
