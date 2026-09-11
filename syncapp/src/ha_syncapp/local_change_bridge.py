"""Bounded owner-thread bridge from local source events to debounce scheduling."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .local_change_debounce import LocalChangeDebouncer
from .local_change_source import LocalChangeSnapshot, observe_local_change_source
from .state import WorkItem


class LocalChangeBridgeError(RuntimeError):
    """A local source event could not be bridged safely."""


LocalChangeObserver = Callable[[Path], LocalChangeSnapshot]


class LocalChangeBridge:
    """Translate bounded source events into debounced normal Local work."""

    def __init__(
        self,
        source: Path,
        debouncer: LocalChangeDebouncer,
        *,
        observer: LocalChangeObserver = observe_local_change_source,
    ) -> None:
        if not isinstance(source, Path):
            raise LocalChangeBridgeError("local change source path is invalid")
        if not isinstance(debouncer, LocalChangeDebouncer):
            raise LocalChangeBridgeError("local change debouncer is invalid")
        if not callable(observer):
            raise LocalChangeBridgeError("local change observer is invalid")

        self._source = source
        self._debouncer = debouncer
        self._observer = observer
        self._snapshot = self._observe()

    def notify_source_event(self, now: float) -> bool:
        """Observe one transport event and notify debounce only for a real routed change."""
        observed = self._observe()
        if observed == self._snapshot:
            return False

        self._snapshot = observed
        self._debouncer.notify(now)
        return True

    def tick(self, now: float) -> WorkItem | None:
        """Advance the existing quiet-period scheduler on the owner thread."""
        return self._debouncer.tick(now)

    def _observe(self) -> LocalChangeSnapshot:
        try:
            return self._observer(self._source)
        except Exception as exc:
            raise LocalChangeBridgeError("local change observation failed closed") from exc
