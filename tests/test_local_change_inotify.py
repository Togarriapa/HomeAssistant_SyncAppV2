import threading
import time
from pathlib import Path
from struct import Struct

import pytest
from ha_syncapp.local_change_inotify import (
    LocalChangeInotifyError,
    _validate_event_payload,
    consume_local_change_events,
)
from ha_syncapp.local_change_mailbox import LocalChangeMailbox
from ha_syncapp.local_change_worker import LocalChangeWorker, LocalChangeWorkerReason


def _wait_for_change(mailbox: LocalChangeMailbox, timeout_seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if mailbox.drain().changed:
            return True
        time.sleep(0.01)
    return False


def _write_until_change(path: Path, mailbox: LocalChangeMailbox) -> None:
    deadline = time.monotonic() + 2.0
    value = 0
    while time.monotonic() < deadline:
        value += 1
        path.write_text(f"value: {value}\n")
        if _wait_for_change(mailbox, 0.05):
            return
    pytest.fail("inotify worker did not forward a filesystem change")


def test_inotify_worker_forwards_nested_existing_directory_changes(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    nested = source / ".storage"
    nested.mkdir(parents=True)
    target = nested / "core.entity_registry"
    target.write_text("{}\n")

    mailbox = LocalChangeMailbox()
    worker = LocalChangeWorker(mailbox, source, consumer=consume_local_change_events)
    worker.start()
    try:
        _write_until_change(target, mailbox)
    finally:
        worker.request_stop()
        result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.STOPPED
    assert result.events_forwarded >= 1


def test_inotify_refreshes_watches_after_new_directory_event(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")

    mailbox = LocalChangeMailbox()
    worker = LocalChangeWorker(mailbox, source, consumer=consume_local_change_events)
    worker.start()
    try:
        new_directory = source / "packages"
        new_directory.mkdir()
        assert _wait_for_change(mailbox) is True

        target = new_directory / "lights.yaml"
        _write_until_change(target, mailbox)
    finally:
        worker.request_stop()
        result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.STOPPED
    assert result.events_forwarded >= 2


def test_inotify_replaces_invalidated_watch_when_directory_path_is_recreated(
    tmp_path: Path,
) -> None:
    source = tmp_path / "homeassistant"
    nested = source / "packages"
    nested.mkdir(parents=True)
    target = nested / "lights.yaml"
    target.write_text("light: first\n")

    mailbox = LocalChangeMailbox()
    worker = LocalChangeWorker(mailbox, source, consumer=consume_local_change_events)
    worker.start()
    try:
        target.unlink()
        nested.rmdir()
        assert _wait_for_change(mailbox) is True

        nested.mkdir()
        assert _wait_for_change(mailbox) is True
        # Let the parent-directory event refresh the recursive watch set, then
        # discard that signal so only activity inside the replacement proves it.
        time.sleep(0.05)
        while mailbox.drain().changed:
            pass

        replacement = nested / "replacement.yaml"
        _write_until_change(replacement, mailbox)
    finally:
        worker.request_stop()
        result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.STOPPED
    assert result.events_forwarded >= 3


def test_inotify_worker_stops_without_filesystem_activity(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()

    mailbox = LocalChangeMailbox()
    worker = LocalChangeWorker(mailbox, source, consumer=consume_local_change_events)
    worker.start()
    worker.request_stop()

    result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.STOPPED
    assert result.events_forwarded == 0
    assert mailbox.drain().changed is False


def test_inotify_payload_reports_only_invalidated_watch_identities() -> None:
    event = Struct("iIII")
    payload = b"".join(
        (
            event.pack(11, 0x00000002, 0, 0),
            event.pack(12, 0x00000400, 0, 0),
            event.pack(13, 0x00008000, 0, 0),
        )
    )

    assert _validate_event_payload(payload) == frozenset({12, 13})


def test_inotify_worker_fails_closed_for_symlink_source(tmp_path: Path) -> None:
    real_source = tmp_path / "real-homeassistant"
    real_source.mkdir()
    source = tmp_path / "homeassistant"
    source.symlink_to(real_source, target_is_directory=True)

    mailbox = LocalChangeMailbox()
    worker = LocalChangeWorker(mailbox, source, consumer=consume_local_change_events)
    worker.start()

    result = worker.join(timeout_seconds=2.0)

    assert result.reason is LocalChangeWorkerReason.FAILED
    assert result.events_forwarded == 0
    assert mailbox.drain().changed is False


def test_inotify_rejects_invalid_source_contract() -> None:
    stop = threading.Event()

    with pytest.raises(LocalChangeInotifyError, match="source is invalid"):
        consume_local_change_events(
            "not-a-path",  # type: ignore[arg-type]
            lambda: None,
            stop,
            lambda: None,
        )
