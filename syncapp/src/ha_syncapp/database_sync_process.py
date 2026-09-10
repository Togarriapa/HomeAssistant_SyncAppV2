"""Bounded normal processor for one durable Recorder database synchronization item."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .database_sync_work import (
    DatabaseSyncWorkError,
    DatabaseSyncWorkResult,
    claim_database_sync_work,
    database_sync_work_key,
    execute_claimed_database_sync_work,
)
from .state import StateError, StateStore


class DatabaseSyncProcessError(RuntimeError):
    """Normal Recorder database synchronization processing failed closed."""


@dataclass(frozen=True, slots=True)
class DatabaseSyncProcessResult:
    """Sanitized result from processing at most one database item."""

    processed: DatabaseSyncWorkResult | None


def run_database_sync_process(
    store: StateStore,
    source_database: Path,
    database_staging_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    github_token: str,
) -> DatabaseSyncProcessResult:
    """Claim and process at most one database item without recovery semantics."""
    if type(store) is not StateStore:
        raise DatabaseSyncProcessError("database synchronization state store is invalid")

    try:
        item = claim_database_sync_work(store)
        if item is None:
            return DatabaseSyncProcessResult(processed=None)

        if item.work_key != database_sync_work_key(target, source_database):
            blocked = store.fail_work(item, transient=False)
            return DatabaseSyncProcessResult(processed=DatabaseSyncWorkResult(blocked, None))

        processed = execute_claimed_database_sync_work(
            store,
            item,
            source_database,
            database_staging_root,
            snapshot_staging_root,
            workspace_root,
            target,
            github_token,
        )
        return DatabaseSyncProcessResult(processed=processed)
    except (StateError, DatabaseSyncWorkError) as exc:
        raise DatabaseSyncProcessError(
            "database synchronization processing failed closed"
        ) from exc
