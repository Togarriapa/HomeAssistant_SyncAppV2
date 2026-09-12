"""Bounded normal-service orchestration for periodic logs publication."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .log_collection import (
    LogCollectionError,
    LogCollectionResult,
    collect_and_enqueue_supervisor_logs,
)
from .log_sync_process import (
    LogSyncProcessError,
    LogSyncProcessResult,
    run_log_sync_process,
)
from .state import StateStore


class LogSyncServiceError(RuntimeError):
    """Routine log service orchestration failed closed."""


@dataclass(frozen=True, slots=True)
class LogSyncTickResult:
    """Sanitized result from one bounded service tick."""

    due: bool
    collection: LogCollectionResult | None
    processed: LogSyncProcessResult | None


class LogSyncService:
    """Collect and process at most one routine logs generation when due."""

    def __init__(
        self,
        store: StateStore,
        artifact_root: Path,
        snapshot_staging_root: Path,
        workspace_root: Path,
        target: str,
        github_token: str,
        *,
        core_token: str | None,
        interval_seconds: float,
    ) -> None:
        if type(store) is not StateStore:
            raise LogSyncServiceError("logs service state store is invalid")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise LogSyncServiceError("logs service interval is invalid")
        self._store = store
        self._artifact_root = artifact_root
        self._snapshot_staging_root = snapshot_staging_root
        self._workspace_root = workspace_root
        self._target = target
        self._github_token = github_token
        self._core_token = core_token
        self._interval_seconds = float(interval_seconds)
        self._next_due: float | None = None
        self._last_now: float | None = None
        self._stopped = False

    def start(self, now: float) -> None:
        """Arm the first routine collection deadline without collecting immediately."""
        current = self._advance_clock(now)
        if self._next_due is not None or self._stopped:
            raise LogSyncServiceError("logs service is already started")
        self._next_due = current + self._interval_seconds

    def tick(self, now: float) -> LogSyncTickResult:
        """Collect once and process at most one durable logs item when due."""
        current = self._advance_clock(now)
        if self._next_due is None or self._stopped:
            raise LogSyncServiceError("logs service is not started")
        if current < self._next_due:
            return LogSyncTickResult(due=False, collection=None, processed=None)

        # Advance from the observed time rather than replaying elapsed periods. A
        # delayed owner loop therefore produces one bounded collection, not a
        # catch-up storm of historical intervals.
        self._next_due = current + self._interval_seconds
        try:
            collection = collect_and_enqueue_supervisor_logs(
                self._store,
                self._artifact_root,
                self._target,
                reference_time=datetime.now(UTC),
                token=self._core_token,
            )
            processed = run_log_sync_process(
                self._store,
                self._artifact_root,
                self._snapshot_staging_root,
                self._workspace_root,
                self._target,
                self._github_token,
            )
        except (LogCollectionError, LogSyncProcessError) as exc:
            raise LogSyncServiceError("logs service tick failed closed") from exc
        return LogSyncTickResult(due=True, collection=collection, processed=processed)

    def stop(self) -> None:
        """Disarm future collection before the owner releases durable state."""
        if self._next_due is None or self._stopped:
            raise LogSyncServiceError("logs service is not started")
        self._stopped = True
        self._next_due = None

    def _advance_clock(self, now: float) -> float:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            raise LogSyncServiceError("logs service clock is invalid")
        current = float(now)
        if self._last_now is not None and current < self._last_now:
            raise LogSyncServiceError("logs service clock moved backwards")
        self._last_now = current
        return current
