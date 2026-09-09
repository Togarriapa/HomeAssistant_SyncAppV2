"""One bounded recovery pass for durable Recorder database synchronization work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ha_syncapp.database_sync_work import (
    DatabaseSyncWorkError,
    DatabaseSyncWorkResult,
    claim_database_sync_work,
    database_sync_work_key,
    execute_claimed_database_sync_work,
)
from ha_syncapp.state import StateError, StateStore


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
    """Recover interrupted work and process at most one eligible database item."""
    if type(store) is not StateStore:
        raise DatabaseSyncRetriggerError("database synchronization retrigger state store is invalid")

    try:
        recovered = store.recover_interrupted_work()
        item = claim_database_sync_work(store)
        if item is None:
            return DatabaseSyncRetriggerResult(recovered_interrupted=recovered, processed=None)
        if item.work_key != database_sync_work_key(target, source_database):
            blocked = store.fail_work(item, transient=False)
            return DatabaseSyncRetriggerResult(
                recovered_interrupted=recovered,
                processed=DatabaseSyncWorkResult(blocked, None),
            )
        processed = execute_claimed_database_sync_work(
            store,
            item,
            source_database,
            database_staging_root,
            snapshot_staging_root,
            workspace_root,
            target,
            token,
        )
        return DatabaseSyncRetriggerResult(recovered_interrupted=recovered, processed=processed)
    except (StateError, DatabaseSyncWorkError) as exc:
        raise DatabaseSyncRetriggerError("database synchronization retrigger pass failed closed") from exc
