"""One normal Recorder database synchronization bootstrap for service startup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .database_sync_process import (
    DatabaseSyncProcessError,
    DatabaseSyncProcessResult,
    run_database_sync_process,
)
from .database_sync_schedule import (
    DatabaseSyncScheduleError,
    schedule_database_sync_generation,
)
from .state import StateStore, WorkItem


class DatabaseStartupError(RuntimeError):
    """The startup Recorder database synchronization failed closed."""


@dataclass(frozen=True, slots=True)
class DatabaseStartupResult:
    """Sanitized outcome from one startup Recorder synchronization bootstrap."""

    scheduled: WorkItem
    processed: DatabaseSyncProcessResult


def run_startup_database_sync(
    store: StateStore,
    source_database: Path,
    database_staging_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    github_token: str,
) -> DatabaseStartupResult:
    """Schedule one normal database generation and process at most one eligible item."""
    if type(store) is not StateStore:
        raise DatabaseStartupError("startup database state store is invalid")

    try:
        scheduled = schedule_database_sync_generation(store, target, source_database)
        processed = run_database_sync_process(
            store,
            source_database,
            database_staging_root,
            snapshot_staging_root,
            workspace_root,
            target,
            github_token,
        )
    except (DatabaseSyncScheduleError, DatabaseSyncProcessError) as exc:
        raise DatabaseStartupError("startup database synchronization failed closed") from exc

    return DatabaseStartupResult(scheduled=scheduled, processed=processed)
