import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from ha_syncapp.local_change_mailbox import LocalChangeMailbox
from ha_syncapp.local_change_worker import (
    LocalChangeWorker,
    LocalChangeWorkerError,
    LocalChangeWorkerReason,
)


def test_worker_forwards_event_burst_into_bounded_mailbox(tmp_path: Path) -> None:
    mailbox = LocalChangeMailbox()
    source = tmp_path / "homeassistant"
    source.mkdir()

    def consumer(
        observed_source: Path,
        notify: Callable[[], None],
        stop: threading.Event,
        ready: Callable[[], None],
    ) -> int:
        del stop
        assert observed_source == source
        ready()
        for _ in range(100):
            notify()
        return 100

    worker = LocalChangeWorker(mailbox, source, consumer=consumer)
    worker.start()
    result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.COMPLETED
    assert result.events_forwarded == 100
    assert mailbox.drain().changed is True
    assert mailbox.drain().changed is False


def test_worker_stops_cooperatively_without_closing_mailbox(tmp_path: Path) -> None:
    mailbox = LocalChangeMailbox()
    source = tmp_path / "homeassistant"
    source.mkdir()
    entered = threading.Event()

    def consumer(
        observed_source: Path,
        notify: Callable[[], None],
        stop: threading.Event,
        ready: Callable[[], None],
    ) -> int:
        del observed_source, notify
        ready()
        entered.set()
        stop.wait(timeout=2.0)
        return 0

    worker = LocalChangeWorker(mailbox, source, consumer=consumer)
    worker.start()
    assert entered.wait(timeout=1.0) is True
    worker.request_stop()
    result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.STOPPED
    assert result.events_forwarded == 0
    mailbox.notify()
    assert mailbox.drain().changed is True


def test_worker_fails_closed_when_transport_raises(tmp_path: Path) -> None:
    mailbox = LocalChangeMailbox()
    source = tmp_path / "homeassistant"
    source.mkdir()

    def consumer(
        observed_source: Path,
        notify: Callable[[], None],
        stop: threading.Event,
        ready: Callable[[], None],
    ) -> int:
        del observed_source, notify, stop, ready
        raise OSError("transport unavailable")

    worker = LocalChangeWorker(mailbox, source, consumer=consumer)
    worker.start()
    result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.FAILED
    assert result.events_forwarded == 0


def test_worker_rejects_transport_count_mismatch(tmp_path: Path) -> None:
    mailbox = LocalChangeMailbox()
    source = tmp_path / "homeassistant"
    source.mkdir()

    def consumer(
        observed_source: Path,
        notify: Callable[[], None],
        stop: threading.Event,
        ready: Callable[[], None],
    ) -> int:
        del observed_source, stop
        ready()
        notify()
        return 0

    worker = LocalChangeWorker(mailbox, source, consumer=consumer)
    worker.start()
    result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.FAILED
    assert result.events_forwarded == 1
    assert mailbox.drain().changed is True


def test_worker_rejects_consumer_that_never_reports_ready(tmp_path: Path) -> None:
    mailbox = LocalChangeMailbox()
    source = tmp_path / "homeassistant"
    source.mkdir()

    def consumer(
        observed_source: Path,
        notify: Callable[[], None],
        stop: threading.Event,
        ready: Callable[[], None],
    ) -> int:
        del observed_source, notify, stop, ready
        return 0

    worker = LocalChangeWorker(mailbox, source, consumer=consumer)
    worker.start()
    result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.FAILED
    assert result.events_forwarded == 0


def test_worker_enforces_single_start_and_bounded_join(tmp_path: Path) -> None:
    mailbox = LocalChangeMailbox()
    source = tmp_path / "homeassistant"
    source.mkdir()

    def consumer(
        observed_source: Path,
        notify: Callable[[], None],
        stop: threading.Event,
        ready: Callable[[], None],
    ) -> int:
        del observed_source, notify, stop
        ready()
        return 0

    worker = LocalChangeWorker(mailbox, source, consumer=consumer)
    worker.start()

    with pytest.raises(LocalChangeWorkerError, match="already started"):
        worker.start()
    with pytest.raises(LocalChangeWorkerError, match="join timeout"):
        worker.join(timeout_seconds=0.0)

    assert worker.join(timeout_seconds=2.0).reason is LocalChangeWorkerReason.COMPLETED
