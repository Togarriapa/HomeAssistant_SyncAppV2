"""One bounded recovery pass for durable Recorder database synchronization work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .database_sync_process import DatabaseSyncProcessError, run_database_sync_process
from .database_sync_work import DatabaseSyncWorkResult
from .state import StateError, StateStore


class DatabaseSyncRetriggerError(RuntimeError):
    """A bounded database recovery pass could not complete safely."""


@dataclass(frozen=True, slots=True)
class DatabaseSyncRetriggerResult:
    """Sanitized outcome of one bounded database recovery pass."""

    recovered_interrupted: int
    processed: DatabaseSyncWorkResult | None


def run_database_sync_retrigger_pass(
    store: StateStore,
    source_database: Path,
    database_staging_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
) -> DatabaseSyncRetriggerResult:
    """Recover interrupted work, then delegate one normal database processing attempt."""
    if type(store) is not StateStore:
        raise DatabaseSyncRetriggerError(
            "database synchronization retrigger state store is invalid"
        )

    try:
        recovered = store.recover_interrupted_work()
        processed = run_database_sync_process(
            store,
            source_database,
            database_staging_root,
            snapshot_staging_root,
            workspace_root,
            target,
            token,
        )
        return DatabaseSyncRetriggerResult(
            recovered_interrupted=recovered,
            processed=processed.processed,
        )
    except (StateError, DatabaseSyncProcessError) as exc:
        raise DatabaseSyncRetriggerError(
            "database synchronization retrigger pass failed closed"
        ) from exc
