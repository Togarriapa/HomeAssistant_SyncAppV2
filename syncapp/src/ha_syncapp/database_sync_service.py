"""Bounded normal-service orchestration for periodic Recorder publication."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .database_sync_process import (
    DatabaseSyncProcessError,
    DatabaseSyncProcessResult,
    run_database_sync_process,
)
from .database_sync_schedule import DatabaseSyncScheduleError, schedule_database_sync_generation
from .state import StateStore


class DatabaseSyncServiceError(RuntimeError):
    """Routine Recorder service orchestration failed closed."""


@dataclass(frozen=True, slots=True)
class DatabaseSyncTickResult:
    """Sanitized result from one bounded service tick."""

    due: bool
    processed: DatabaseSyncProcessResult | None


class DatabaseSyncService:
    """Schedule and process at most one routine Recorder generation when due."""

    def __init__(
        self,
        store: StateStore,
        source_database: Path,
        database_staging_root: Path,
        snapshot_staging_root: Path,
        workspace_root: Path,
        target: str,
        github_token: str,
        *,
        interval_seconds: float,
    ) -> None:
        if type(store) is not StateStore:
            raise DatabaseSyncServiceError("database service state store is invalid")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise DatabaseSyncServiceError("database service interval is invalid")
        self._store = store
        self._source_database = source_database
        self._database_staging_root = database_staging_root
        self._snapshot_staging_root = snapshot_staging_root
        self._workspace_root = workspace_root
        self._target = target
        self._github_token = github_token
        self._interval_seconds = float(interval_seconds)
        self._next_due: float | None = None
        self._last_now: float | None = None
        self._stopped = False

    def start(self, now: float) -> None:
        """Arm the first routine deadline without scheduling work immediately."""
        current = self._advance_clock(now)
        if self._next_due is not None or self._stopped:
            raise DatabaseSyncServiceError("database service is already started")
        self._next_due = current + self._interval_seconds

    def tick(self, now: float) -> DatabaseSyncTickResult:
        """Run at most one schedule/process transaction when the deadline is due."""
        current = self._advance_clock(now)
        if self._next_due is None or self._stopped:
            raise DatabaseSyncServiceError("database service is not started")
        if current < self._next_due:
            return DatabaseSyncTickResult(due=False, processed=None)

        # Advance from the observation time rather than replaying missed intervals.
        # This makes a delayed owner loop coalesce elapsed periods into one bounded
        # generation instead of creating a catch-up storm.
        self._next_due = current + self._interval_seconds
        try:
            schedule_database_sync_generation(
                self._store,
                self._target,
                self._source_database,
            )
            processed = run_database_sync_process(
                self._store,
                self._source_database,
                self._database_staging_root,
                self._snapshot_staging_root,
                self._workspace_root,
                self._target,
                self._github_token,
            )
        except (DatabaseSyncScheduleError, DatabaseSyncProcessError) as exc:
            raise DatabaseSyncServiceError("database service tick failed closed") from exc
        return DatabaseSyncTickResult(due=True, processed=processed)

    def stop(self) -> None:
        """Disarm future scheduling before the owner releases durable state."""
        if self._next_due is None or self._stopped:
            raise DatabaseSyncServiceError("database service is not started")
        self._stopped = True
        self._next_due = None

    def _advance_clock(self, now: float) -> float:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            raise DatabaseSyncServiceError("database service clock is invalid")
        current = float(now)
        if self._last_now is not None and current < self._last_now:
            raise DatabaseSyncServiceError("database service clock moved backwards")
        self._last_now = current
        return current
