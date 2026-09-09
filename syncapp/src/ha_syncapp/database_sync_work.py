"""Durable work-state adapter for guarded Recorder database publication."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ha_syncapp.database_sync import (
    DatabaseSyncDisposition,
    DatabaseSyncError,
    DatabaseSyncResult,
    synchronize_database_snapshot,
)
from ha_syncapp.state import StateError, StateStore, WorkItem

_WORK_KIND = "database"
_BLOCKING_DISPOSITIONS = {
    DatabaseSyncDisposition.BASELINE_REQUIRED,
    DatabaseSyncDisposition.DIVERGED,
    DatabaseSyncDisposition.REMOTE_MISSING,
}
_SUCCESS_DISPOSITIONS = {
    DatabaseSyncDisposition.INITIALIZED,
    DatabaseSyncDisposition.PUBLISHED,
    DatabaseSyncDisposition.NO_CHANGE,
}


class DatabaseSyncWorkError(RuntimeError):
    """A durable database-sync item cannot be executed or transitioned safely."""


@dataclass(frozen=True, slots=True)
class DatabaseSyncWorkResult:
    """Persisted work outcome plus guarded database synchronization evidence."""

    work: WorkItem
    synchronization: DatabaseSyncResult | None


def database_sync_work_key(target: str, source_database: Path) -> str:
    """Return a deterministic identity for one Repo B target and Recorder path."""
    if not isinstance(target, str) or not target:
        raise DatabaseSyncWorkError("database synchronization work identity is invalid")
    if type(source_database) is not Path or not source_database.is_absolute():
        raise DatabaseSyncWorkError("database synchronization source path is invalid")
    payload = f"{target.casefold()}\0{source_database.as_posix()}".encode()
    return hashlib.sha256(payload).hexdigest()


def enqueue_database_sync_work(
    store: StateStore,
    target: str,
    source_database: Path,
) -> WorkItem:
    """Idempotently enqueue one guarded Recorder publication unit."""
    if type(store) is not StateStore:
        raise DatabaseSyncWorkError("database synchronization work state store is invalid")
    try:
        return store.enqueue_work(_WORK_KIND, database_sync_work_key(target, source_database))
    except StateError as exc:
        raise DatabaseSyncWorkError("database synchronization work could not be enqueued") from exc


def claim_database_sync_work(
    store: StateStore,
    *,
    now: datetime | None = None,
) -> WorkItem | None:
    """Atomically claim only the oldest eligible database synchronization item."""
    if type(store) is not StateStore:
        raise DatabaseSyncWorkError("database synchronization work state store is invalid")
    try:
        return store.claim_work_kind(_WORK_KIND, now=now)
    except StateError as exc:
        raise DatabaseSyncWorkError("database synchronization work claim is invalid") from exc


def execute_claimed_database_sync_work(
    store: StateStore,
    item: WorkItem,
    source_database: Path,
    database_staging_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
) -> DatabaseSyncWorkResult:
    """Execute one already-claimed database item and durably record its outcome."""
    _validate_claim(store, item, target, source_database)
    try:
        synchronization = synchronize_database_snapshot(
            store,
            source_database,
            database_staging_root,
            snapshot_staging_root,
            workspace_root,
            target,
            token,
        )
    except DatabaseSyncError:
        try:
            failed = store.fail_work(item, transient=True)
        except StateError as exc:
            raise DatabaseSyncWorkError(
                "database synchronization retry state could not be recorded"
            ) from exc
        return DatabaseSyncWorkResult(failed, None)

    try:
        if synchronization.disposition in _SUCCESS_DISPOSITIONS:
            transitioned = store.complete_work(item)
        elif synchronization.disposition in _BLOCKING_DISPOSITIONS:
            transitioned = store.fail_work(item, transient=False)
        else:
            raise DatabaseSyncWorkError("database synchronization returned an unknown disposition")
    except StateError as exc:
        raise DatabaseSyncWorkError(
            "database synchronization work outcome could not be recorded"
        ) from exc
    return DatabaseSyncWorkResult(transitioned, synchronization)


def _validate_claim(
    store: StateStore,
    item: WorkItem,
    target: str,
    source_database: Path,
) -> None:
    if type(store) is not StateStore:
        raise DatabaseSyncWorkError("database synchronization work state store is invalid")
    if type(item) is not WorkItem:
        raise DatabaseSyncWorkError("database synchronization work evidence is invalid")
    if item.work_kind != _WORK_KIND or item.status != "running" or item.attempts < 1:
        raise DatabaseSyncWorkError("database synchronization work is not an eligible claimed item")
    if item.work_key != database_sync_work_key(target, source_database):
        raise DatabaseSyncWorkError(
            "database synchronization work identity does not match the target"
        )
