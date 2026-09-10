"""Thread-isolated Home Assistant runtime event transport with bounded reconnects."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from .core_event_stream import CoreEventStreamError, consume_core_runtime_events
from .runtime_event_mailbox import RuntimeEventMailbox, RuntimeEventMailboxError

_DEFAULT_MAX_ATTEMPTS: Final = 5
_DEFAULT_INITIAL_BACKOFF_SECONDS: Final = 1.0
_DEFAULT_MAX_BACKOFF_SECONDS: Final = 30.0
_DEFAULT_JOIN_TIMEOUT_SECONDS: Final = 10.0
_MAX_ATTEMPTS: Final = 20
_MAX_BACKOFF_SECONDS: Final = 300.0
_MAX_JOIN_TIMEOUT_SECONDS: Final = 60.0


class RuntimeEventWorkerError(RuntimeError):
    """The isolated runtime event worker could not operate safely."""


class RuntimeEventWorkerConsumer(Protocol):
    """Minimum Core event-consumer surface required by the worker."""

    async def __call__(
        self,
        handler: Callable[[Mapping[str, object]], Awaitable[None]],
        *,
        on_ready: Callable[[], Awaitable[None]] | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int: ...


class RuntimeEventWorkerReason(StrEnum):
    """Sanitized terminal classes exposed outside the transport thread."""

    STOPPED = "stopped"
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeEventWorkerResult:
    """Bounded transport metadata; contains no event payload or credential material."""

    reason: RuntimeEventWorkerReason
    attempts: int
    reconnects: int
    events_forwarded: int
    readiness_signals: int


class RuntimeEventWorker:
    """Own the async Core event transport without ever receiving ``StateStore``."""

    def __init__(
        self,
        mailbox: RuntimeEventMailbox,
        *,
        token: str | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        initial_backoff_seconds: float = _DEFAULT_INITIAL_BACKOFF_SECONDS,
        max_backoff_seconds: float = _DEFAULT_MAX_BACKOFF_SECONDS,
        consumer: RuntimeEventWorkerConsumer = consume_core_runtime_events,
    ) -> None:
        _validate_configuration(
            mailbox,
            max_attempts,
            initial_backoff_seconds,
            max_backoff_seconds,
            consumer,
        )
        self._mailbox = mailbox
        self._token = token
        self._max_attempts = max_attempts
        self._initial_backoff_seconds = initial_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._consumer = consumer
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._stop_requested = threading.Event()
        self._result: RuntimeEventWorkerResult | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start exactly one worker thread."""
        with self._lock:
            if self._thread is not None:
                raise RuntimeEventWorkerError("runtime event worker was already started")
            thread = threading.Thread(
                target=self._thread_main,
                name="ha-syncapp-runtime-events",
                daemon=False,
            )
            self._thread = thread
            thread.start()

    def request_stop(self) -> None:
        """Request cooperative cancellation of active waiting or reconnect backoff."""
        self._stop_requested.set()
        with self._lock:
            loop = self._loop
            stop = self._async_stop
        if loop is not None and stop is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(stop.set)

    def join(
        self,
        *,
        timeout_seconds: float = _DEFAULT_JOIN_TIMEOUT_SECONDS,
    ) -> RuntimeEventWorkerResult:
        """Wait a bounded time for termination and return only sanitized metadata."""
        _validate_join_timeout(timeout_seconds)
        with self._lock:
            thread = self._thread
        if thread is None:
            raise RuntimeEventWorkerError("runtime event worker was not started")
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise RuntimeEventWorkerError("runtime event worker did not stop in time")
        with self._lock:
            result = self._result
        if result is None:
            raise RuntimeEventWorkerError("runtime event worker result is unavailable")
        return result

    def _thread_main(self) -> None:
        try:
            result = asyncio.run(self._run())
        except BaseException:
            result = _result(RuntimeEventWorkerReason.FAILED, 0, 0, 0, 0)
        with self._lock:
            self._loop = None
            self._async_stop = None
            self._result = result

    async def _run(self) -> RuntimeEventWorkerResult:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        with self._lock:
            self._loop = loop
            self._async_stop = stop
        if self._stop_requested.is_set():
            stop.set()

        attempts = 0
        reconnects = 0
        events_forwarded = 0
        readiness_signals = 0
        mailbox_failed = False

        async def forward_event(evidence: Mapping[str, object]) -> None:
            nonlocal events_forwarded, mailbox_failed
            try:
                await self._mailbox.event(evidence)
            except RuntimeEventMailboxError:
                mailbox_failed = True
                raise
            events_forwarded += 1

        async def forward_ready() -> None:
            nonlocal readiness_signals, mailbox_failed
            try:
                await self._mailbox.ready()
            except RuntimeEventMailboxError:
                mailbox_failed = True
                raise
            readiness_signals += 1

        while attempts < self._max_attempts and not stop.is_set():
            attempts += 1
            try:
                stopped = await _consume_until_stop(
                    self._consumer,
                    forward_event,
                    forward_ready,
                    self._token,
                    stop,
                )
            except CoreEventStreamError:
                if mailbox_failed:
                    return _result(
                        RuntimeEventWorkerReason.FAILED,
                        attempts,
                        reconnects,
                        events_forwarded,
                        readiness_signals,
                    )
                if stop.is_set():
                    return _result(
                        RuntimeEventWorkerReason.STOPPED,
                        attempts,
                        reconnects,
                        events_forwarded,
                        readiness_signals,
                    )
                if attempts >= self._max_attempts:
                    return _result(
                        RuntimeEventWorkerReason.EXHAUSTED,
                        attempts,
                        reconnects,
                        events_forwarded,
                        readiness_signals,
                    )
                reconnects += 1
                delay = min(
                    self._initial_backoff_seconds * (2 ** (attempts - 1)),
                    self._max_backoff_seconds,
                )
                if await _wait_for_stop(stop, delay):
                    return _result(
                        RuntimeEventWorkerReason.STOPPED,
                        attempts,
                        reconnects,
                        events_forwarded,
                        readiness_signals,
                    )
                continue
            except Exception:
                return _result(
                    RuntimeEventWorkerReason.FAILED,
                    attempts,
                    reconnects,
                    events_forwarded,
                    readiness_signals,
                )

            reason = (
                RuntimeEventWorkerReason.STOPPED if stopped else RuntimeEventWorkerReason.COMPLETED
            )
            return _result(
                reason,
                attempts,
                reconnects,
                events_forwarded,
                readiness_signals,
            )

        return _result(
            RuntimeEventWorkerReason.STOPPED,
            attempts,
            reconnects,
            events_forwarded,
            readiness_signals,
        )


async def _consume_until_stop(
    consumer: RuntimeEventWorkerConsumer,
    handler: Callable[[Mapping[str, object]], Awaitable[None]],
    on_ready: Callable[[], Awaitable[None]],
    token: str | None,
    stop: asyncio.Event,
) -> bool:
    consumer_task = asyncio.create_task(
        consumer(handler, on_ready=on_ready, token=token, max_events=None)
    )
    stop_task = asyncio.create_task(stop.wait())
    done, pending = await asyncio.wait(
        {consumer_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if stop_task in done:
        consumer_task.cancel()
        with suppress(asyncio.CancelledError):
            await consumer_task
        return True

    stop_task.cancel()
    with suppress(asyncio.CancelledError):
        await stop_task
    result = await consumer_task
    if type(result) is not int or result < 0:
        raise RuntimeEventWorkerError("runtime event worker consumer result is invalid")
    for task in pending:
        task.cancel()
    return False


async def _wait_for_stop(stop: asyncio.Event, delay_seconds: float) -> bool:
    if stop.is_set():
        return True
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay_seconds)
    except TimeoutError:
        return False
    return True


def _result(
    reason: RuntimeEventWorkerReason,
    attempts: int,
    reconnects: int,
    events_forwarded: int,
    readiness_signals: int,
) -> RuntimeEventWorkerResult:
    return RuntimeEventWorkerResult(
        reason=reason,
        attempts=attempts,
        reconnects=reconnects,
        events_forwarded=events_forwarded,
        readiness_signals=readiness_signals,
    )


def _validate_configuration(
    mailbox: RuntimeEventMailbox,
    max_attempts: int,
    initial_backoff_seconds: float,
    max_backoff_seconds: float,
    consumer: RuntimeEventWorkerConsumer,
) -> None:
    if type(mailbox) is not RuntimeEventMailbox:
        raise RuntimeEventWorkerError("runtime event worker mailbox is invalid")
    if type(max_attempts) is not int or not 1 <= max_attempts <= _MAX_ATTEMPTS:
        raise RuntimeEventWorkerError("runtime event worker attempt limit is invalid")
    for value in (initial_backoff_seconds, max_backoff_seconds):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise RuntimeEventWorkerError("runtime event worker backoff is invalid")
        if value <= 0 or value > _MAX_BACKOFF_SECONDS:
            raise RuntimeEventWorkerError("runtime event worker backoff is invalid")
    if initial_backoff_seconds > max_backoff_seconds:
        raise RuntimeEventWorkerError("runtime event worker backoff is invalid")
    if not callable(consumer):
        raise RuntimeEventWorkerError("runtime event worker consumer is invalid")


def _validate_join_timeout(timeout_seconds: float) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
        raise RuntimeEventWorkerError("runtime event worker join timeout is invalid")
    if timeout_seconds <= 0 or timeout_seconds > _MAX_JOIN_TIMEOUT_SECONDS:
        raise RuntimeEventWorkerError("runtime event worker join timeout is invalid")
