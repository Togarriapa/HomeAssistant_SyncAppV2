"""Thread-isolated producer for bounded routine Local configuration change signals."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from .local_change_mailbox import LocalChangeMailbox, LocalChangeMailboxError

_DEFAULT_JOIN_TIMEOUT_SECONDS: Final = 10.0
_MAX_JOIN_TIMEOUT_SECONDS: Final = 60.0


class LocalChangeWorkerError(RuntimeError):
    """The isolated local change producer could not operate safely."""


class LocalChangeWorkerConsumer(Protocol):
    """Minimum filesystem-event transport surface required by the worker."""

    def __call__(
        self,
        source: Path,
        notify: Callable[[], None],
        stop: threading.Event,
    ) -> int: ...


class LocalChangeWorkerReason(StrEnum):
    """Sanitized terminal classes exposed outside the transport thread."""

    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LocalChangeWorkerResult:
    """Bounded transport metadata without file names or configuration contents."""

    reason: LocalChangeWorkerReason
    events_forwarded: int


class LocalChangeWorker:
    """Own filesystem-event transport without ever receiving ``StateStore``."""

    def __init__(
        self,
        mailbox: LocalChangeMailbox,
        source: Path,
        *,
        consumer: LocalChangeWorkerConsumer,
    ) -> None:
        if type(mailbox) is not LocalChangeMailbox:
            raise LocalChangeWorkerError("local change worker mailbox is invalid")
        if not isinstance(source, Path):
            raise LocalChangeWorkerError("local change worker source is invalid")
        if not callable(consumer):
            raise LocalChangeWorkerError("local change worker consumer is invalid")
        self._mailbox = mailbox
        self._source = source
        self._consumer = consumer
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._result: LocalChangeWorkerResult | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start exactly one producer thread."""
        with self._lock:
            if self._thread is not None:
                raise LocalChangeWorkerError("local change worker was already started")
            thread = threading.Thread(
                target=self._thread_main,
                name="ha-syncapp-local-events",
                daemon=False,
            )
            self._thread = thread
            thread.start()

    def request_stop(self) -> None:
        """Request cooperative transport shutdown without touching durable work state."""
        self._stop.set()

    def result_if_finished(self) -> LocalChangeWorkerResult | None:
        """Inspect terminal state without blocking or exposing transport details."""
        with self._lock:
            thread = self._thread
            result = self._result
        if thread is None:
            raise LocalChangeWorkerError("local change worker was not started")
        if thread.is_alive():
            return None
        if result is None:
            raise LocalChangeWorkerError("local change worker result is unavailable")
        return result

    def join(
        self,
        *,
        timeout_seconds: float = _DEFAULT_JOIN_TIMEOUT_SECONDS,
    ) -> LocalChangeWorkerResult:
        """Wait a bounded time for cooperative shutdown."""
        _validate_join_timeout(timeout_seconds)
        with self._lock:
            thread = self._thread
        if thread is None:
            raise LocalChangeWorkerError("local change worker was not started")
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise LocalChangeWorkerError("local change worker did not stop in time")
        with self._lock:
            result = self._result
        if result is None:
            raise LocalChangeWorkerError("local change worker result is unavailable")
        return result

    def _thread_main(self) -> None:
        forwarded = 0

        def notify() -> None:
            nonlocal forwarded
            self._mailbox.notify()
            forwarded += 1

        try:
            consumed = self._consumer(self._source, notify, self._stop)
            if type(consumed) is not int or consumed < 0 or consumed != forwarded:
                raise LocalChangeWorkerError("local change worker consumer result is invalid")
            reason = (
                LocalChangeWorkerReason.STOPPED
                if self._stop.is_set()
                else LocalChangeWorkerReason.COMPLETED
            )
            result = LocalChangeWorkerResult(reason=reason, events_forwarded=forwarded)
        except (LocalChangeMailboxError, Exception):
            result = LocalChangeWorkerResult(
                reason=LocalChangeWorkerReason.FAILED,
                events_forwarded=forwarded,
            )
        with self._lock:
            self._result = result


def _validate_join_timeout(timeout_seconds: float) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
        raise LocalChangeWorkerError("local change worker join timeout is invalid")
    if timeout_seconds <= 0 or timeout_seconds > _MAX_JOIN_TIMEOUT_SECONDS:
        raise LocalChangeWorkerError("local change worker join timeout is invalid")
