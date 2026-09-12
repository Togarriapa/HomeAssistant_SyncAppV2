"""Owner-thread integration for event-driven routine Local synchronization."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .local_change_bridge import (
    LocalChangeBridge,
    LocalChangeBridgeError,
    LocalChangeObserver,
)
from .local_change_debounce import LocalChangeDebounceError, LocalChangeDebouncer
from .local_change_inotify import consume_local_change_events
from .local_change_mailbox import LocalChangeMailbox, LocalChangeMailboxError
from .local_change_worker import (
    LocalChangeWorker,
    LocalChangeWorkerConsumer,
    LocalChangeWorkerError,
    LocalChangeWorkerReason,
)
from .local_sync_process import (
    LocalSyncProcessError,
    LocalSyncProcessResult,
    run_local_sync_process,
)
from .state import StateStore


class LocalChangeServiceError(RuntimeError):
    """Normal local-change service integration failed closed."""


LocalSyncProcessor = Callable[
    [StateStore, Path, Path, Path, str, str],
    LocalSyncProcessResult,
]


class LocalChangeService:
    """Connect bounded filesystem events to the existing durable Local sync path."""

    def __init__(
        self,
        store: StateStore,
        source: Path,
        snapshot_root: Path,
        workspace_root: Path,
        target: str,
        github_token: str,
        *,
        quiet_seconds: float,
        consumer: LocalChangeWorkerConsumer = consume_local_change_events,
        observer: LocalChangeObserver | None = None,
        processor: LocalSyncProcessor = run_local_sync_process,
    ) -> None:
        if type(store) is not StateStore:
            raise LocalChangeServiceError("local change service state store is invalid")
        if not callable(processor):
            raise LocalChangeServiceError("local change service processor is invalid")

        try:
            mailbox = LocalChangeMailbox()
            debouncer = LocalChangeDebouncer(
                store,
                target,
                quiet_seconds=quiet_seconds,
            )
            bridge = (
                LocalChangeBridge(source, debouncer)
                if observer is None
                else LocalChangeBridge(source, debouncer, observer=observer)
            )
            worker = LocalChangeWorker(mailbox, source, consumer=consumer)
        except (
            LocalChangeBridgeError,
            LocalChangeDebounceError,
            LocalChangeWorkerError,
        ) as exc:
            raise LocalChangeServiceError(
                "local change service initialization failed closed"
            ) from exc

        self._store = store
        self._source = source
        self._snapshot_root = snapshot_root
        self._workspace_root = workspace_root
        self._target = target
        self._github_token = github_token
        self._mailbox = mailbox
        self._bridge = bridge
        self._worker = worker
        self._processor = processor
        self._started = False
        self._stopped = False

    def start(self, now: float) -> None:
        """Start event production and close the initial watch-installation race."""
        if self._started or self._stopped:
            raise LocalChangeServiceError("local change service cannot be started")
        try:
            self._worker.start()
            self._bridge.notify_source_event(now)
        except (LocalChangeBridgeError, LocalChangeWorkerError):
            self._stop_worker_after_failed_start()
            raise LocalChangeServiceError(
                "local change service startup failed closed"
            ) from None
        self._started = True

    def tick(self, now: float) -> LocalSyncProcessResult | None:
        """Drain one bounded signal, debounce it, and process at most one durable item."""
        if not self._started or self._stopped:
            raise LocalChangeServiceError("local change service is not active")
        try:
            terminal = self._worker.result_if_finished()
            if terminal is not None:
                raise LocalChangeServiceError(
                    "local change event transport stopped unexpectedly"
                )

            drained = self._mailbox.drain()
            if drained.changed:
                self._bridge.notify_source_event(now)
            scheduled = self._bridge.tick(now)
            if scheduled is None:
                return None
            return self._processor(
                self._store,
                self._source,
                self._snapshot_root,
                self._workspace_root,
                self._target,
                self._github_token,
            )
        except (
            LocalChangeBridgeError,
            LocalChangeDebounceError,
            LocalChangeMailboxError,
            LocalChangeWorkerError,
            LocalSyncProcessError,
        ) as exc:
            raise LocalChangeServiceError(
                "local change service tick failed closed"
            ) from exc

    def stop(self) -> None:
        """Stop event production before relinquishing owner-thread service state."""
        if not self._started or self._stopped:
            raise LocalChangeServiceError("local change service is not active")
        self._worker.request_stop()
        try:
            result = self._worker.join()
            if result.reason is not LocalChangeWorkerReason.STOPPED:
                raise LocalChangeServiceError(
                    "local change event transport did not stop cleanly"
                )
            self._mailbox.close()
        except (LocalChangeMailboxError, LocalChangeWorkerError) as exc:
            raise LocalChangeServiceError(
                "local change service shutdown failed closed"
            ) from exc
        self._stopped = True

    def _stop_worker_after_failed_start(self) -> None:
        self._worker.request_stop()
        try:
            result = self._worker.result_if_finished()
            if result is None:
                self._worker.join()
        except LocalChangeWorkerError:
            pass
