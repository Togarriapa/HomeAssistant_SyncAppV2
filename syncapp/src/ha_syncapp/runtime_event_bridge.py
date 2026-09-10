"""Owner-thread bridge from normalized runtime events to durable runtime synchronization."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .runtime_event_mailbox import (
    RuntimeEventMailbox,
    RuntimeEventMailboxDrainResult,
    RuntimeEventMailboxError,
    drain_runtime_event_mailbox,
)
from .runtime_event_worker import (
    RuntimeEventWorker,
    RuntimeEventWorkerConsumer,
    RuntimeEventWorkerError,
    RuntimeEventWorkerReason,
    RuntimeEventWorkerResult,
)
from .runtime_sync_process import (
    RuntimeSyncProcessError,
    RuntimeSyncProcessResult,
    run_runtime_sync_process,
)
from .state import StateStore


class RuntimeEventBridgeError(RuntimeError):
    """Normal runtime event processing could not continue safely."""


RuntimeProcessor = Callable[
    [StateStore, Path, Path, Path, str, str],
    RuntimeSyncProcessResult,
]


@dataclass(frozen=True, slots=True)
class RuntimeEventBridgeTickResult:
    """Sanitized bounded evidence from one owner-thread bridge tick."""

    mailbox: RuntimeEventMailboxDrainResult
    processing: RuntimeSyncProcessResult


class RuntimeEventBridge:
    """Keep transport isolated while the service owner performs durable work."""

    def __init__(
        self,
        store: StateStore,
        runtime_staging_root: Path,
        snapshot_staging_root: Path,
        workspace_root: Path,
        target: str,
        github_token: str,
        *,
        core_token: str | None = None,
        consumer: RuntimeEventWorkerConsumer | None = None,
        processor: RuntimeProcessor | None = None,
    ) -> None:
        if type(store) is not StateStore:
            raise RuntimeEventBridgeError("runtime event bridge state store is invalid")
        self._store = store
        self._runtime_staging_root = runtime_staging_root
        self._snapshot_staging_root = snapshot_staging_root
        self._workspace_root = workspace_root
        self._target = target
        self._github_token = github_token
        self._core_token = core_token
        self._mailbox = RuntimeEventMailbox()
        worker_arguments: dict[str, object] = {"token": core_token}
        if consumer is not None:
            worker_arguments["consumer"] = consumer
        self._worker = RuntimeEventWorker(self._mailbox, **worker_arguments)  # type: ignore[arg-type]
        self._processor = processor
        self._owner_thread = threading.get_ident()
        self._started = False
        self._stopped = False

    @property
    def mailbox(self) -> RuntimeEventMailbox:
        """Expose the bounded normalized mailbox for deterministic owner-thread tests."""
        return self._mailbox

    def start(self) -> None:
        """Start transport from the StateStore owner thread exactly once."""
        self._assert_owner()
        if self._started or self._stopped:
            raise RuntimeEventBridgeError("runtime event bridge lifecycle is invalid")
        try:
            self._worker.start()
        except RuntimeEventWorkerError as exc:
            raise RuntimeEventBridgeError("runtime event bridge transport could not start") from exc
        self._started = True

    def tick(self) -> RuntimeEventBridgeTickResult:
        """Drain bounded signals and process at most one eligible durable runtime item."""
        self._assert_active()
        self._fail_if_worker_terminated()
        try:
            drained = drain_runtime_event_mailbox(self._mailbox, self._store, self._target)
            processor = self._processor
            if processor is None:
                processed = run_runtime_sync_process(
                    self._store,
                    self._runtime_staging_root,
                    self._snapshot_staging_root,
                    self._workspace_root,
                    self._target,
                    self._github_token,
                    core_token=self._core_token,
                )
            else:
                processed = processor(
                    self._store,
                    self._runtime_staging_root,
                    self._snapshot_staging_root,
                    self._workspace_root,
                    self._target,
                    self._github_token,
                )
        except (RuntimeEventMailboxError, RuntimeSyncProcessError) as exc:
            raise RuntimeEventBridgeError("runtime event bridge tick failed closed") from exc
        self._fail_if_worker_terminated()
        return RuntimeEventBridgeTickResult(mailbox=drained, processing=processed)

    def stop(self, *, timeout_seconds: float = 10.0) -> RuntimeEventWorkerResult:
        """Stop transport cooperatively and join it with a bounded timeout."""
        self._assert_owner()
        if not self._started or self._stopped:
            raise RuntimeEventBridgeError("runtime event bridge lifecycle is invalid")
        try:
            self._worker.request_stop()
            result = self._worker.join(timeout_seconds=timeout_seconds)
        except RuntimeEventWorkerError as exc:
            raise RuntimeEventBridgeError("runtime event bridge transport did not stop safely") from exc
        self._stopped = True
        if result.reason is not RuntimeEventWorkerReason.STOPPED:
            raise RuntimeEventBridgeError("runtime event bridge transport terminated unexpectedly")
        return result

    def _fail_if_worker_terminated(self) -> None:
        try:
            result = self._worker.result_if_finished()
        except RuntimeEventWorkerError as exc:
            raise RuntimeEventBridgeError("runtime event bridge transport state is invalid") from exc
        if result is not None:
            raise RuntimeEventBridgeError("runtime event bridge transport terminated unexpectedly")

    def _assert_active(self) -> None:
        self._assert_owner()
        if not self._started or self._stopped:
            raise RuntimeEventBridgeError("runtime event bridge lifecycle is invalid")

    def _assert_owner(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeEventBridgeError("runtime event bridge must run on its owner thread")
