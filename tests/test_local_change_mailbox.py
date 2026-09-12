import threading

import pytest
from ha_syncapp.local_change_mailbox import LocalChangeMailbox, LocalChangeMailboxError


def test_mailbox_coalesces_event_burst_into_one_pending_signal() -> None:
    mailbox = LocalChangeMailbox()

    for _ in range(100):
        mailbox.notify()

    assert mailbox.drain().changed is True
    assert mailbox.drain().changed is False


def test_mailbox_preserves_change_arriving_after_drain() -> None:
    mailbox = LocalChangeMailbox()

    mailbox.notify()
    assert mailbox.drain().changed is True

    mailbox.notify()
    assert mailbox.drain().changed is True
    assert mailbox.drain().changed is False


def test_mailbox_accepts_concurrent_producer_without_queue_growth() -> None:
    mailbox = LocalChangeMailbox()
    start = threading.Event()

    def producer() -> None:
        start.wait()
        for _ in range(1000):
            mailbox.notify()

    thread = threading.Thread(target=producer)
    thread.start()
    start.set()
    thread.join(timeout=2.0)

    assert thread.is_alive() is False
    assert mailbox.drain().changed is True
    assert mailbox.drain().changed is False


def test_close_discards_pending_signal_and_fails_closed() -> None:
    mailbox = LocalChangeMailbox()
    mailbox.notify()
    mailbox.close()

    with pytest.raises(LocalChangeMailboxError, match="closed"):
        mailbox.notify()
    with pytest.raises(LocalChangeMailboxError, match="closed"):
        mailbox.drain()
    with pytest.raises(LocalChangeMailboxError, match="already closed"):
        mailbox.close()
