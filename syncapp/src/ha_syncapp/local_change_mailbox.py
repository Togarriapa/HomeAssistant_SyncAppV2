"""Bounded cross-thread mailbox for routine Local configuration change signals."""

from __future__ import annotations

import threading
from dataclasses import dataclass


class LocalChangeMailboxError(RuntimeError):
    """Local change mailbox state is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class LocalChangeMailboxDrainResult:
    """Bounded evidence from draining normalized local change signals."""

    changed: bool


class LocalChangeMailbox:
    """Collapse any event burst into one pending owner-thread notification."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._changed = False
        self._closed = False

    def notify(self) -> None:
        """Record that at least one source event occurred without queue growth."""
        with self._lock:
            if self._closed:
                raise LocalChangeMailboxError("local change mailbox is closed")
            self._changed = True

    def drain(self) -> LocalChangeMailboxDrainResult:
        """Consume the current pending bit exactly once."""
        with self._lock:
            if self._closed:
                raise LocalChangeMailboxError("local change mailbox is closed")
            changed = self._changed
            self._changed = False
        return LocalChangeMailboxDrainResult(changed=changed)

    def close(self) -> None:
        """Prevent any further producer or consumer use."""
        with self._lock:
            if self._closed:
                raise LocalChangeMailboxError("local change mailbox is already closed")
            self._closed = True
            self._changed = False
