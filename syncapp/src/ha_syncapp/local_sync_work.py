"""Durable work-state adapter for guarded Local -> Repo B synchronization."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ha_syncapp.local_sync import (
    LocalSyncDisposition,
    LocalSyncError,
    LocalSyncResult,
    synchronize_local_configuration,
)
from ha_syncapp.state import StateError, StateStore, WorkItem

_WORK_KIND = "local_sync"
_BLOCKING_DISPOSITIONS = {
    LocalSyncDisposition.BASELINE_REQUIRED,
    LocalSyncDisposition.DIVERGED,
    LocalSyncDisposition.REMOTE_MISSING,
}
_SUCCESS_DISPOSITIONS = {
    LocalSyncDisposition.INITIALIZED,
    LocalSyncDisposition.PUBLISHED,
    LocalSyncDisposition.NO_CHANGE,
}


class LocalSyncWorkError(RuntimeError):
    """A durable Local-sync work item cannot be executed or transitioned safely."""


@dataclass(frozen=True, slots=True)
class LocalSyncWorkResult:
    work: WorkItem
    synchronization: LocalSyncResult | None


def local_sync_work_key(target: str, branch: str = "main") -> str:
    """Return a deterministic bounded identity for one repository branch."""
    if not isinstance(target, str) or not target or not isinstance(branch, str) or not branch:
        raise LocalSyncWorkError("local synchronization work identity is invalid")
    payload = f"{target.casefold()}\0{branch}".encode()
    return hashlib.sha256(payload).hexdigest()


def enqueue_local_sync_work(
    store: StateStore,
    target: str,
    *,
    branch: str = "main",
) -> WorkItem:
    """Idempotently enqueue one Local-sync unit without claiming unrelated work."""
    if type(store) is not StateStore:
        raise LocalSyncWorkError("local synchronization work state store is invalid")
    try:
        return store.enqueue_work(_WORK_KIND, local_sync_work_key(target, branch))
    except StateError as exc:
        raise LocalSyncWorkError("local synchronization work could not be enqueued") from exc


def claim_local_sync_work(
    store: StateStore,
    *,
    now: datetime | None = None,
) -> WorkItem | None:
    """Atomically claim only the oldest eligible Local-sync item."""
    if type(store) is not StateStore:
        raise LocalSyncWorkError("local synchronization work state store is invalid")
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise LocalSyncWorkError("local synchronization claim time is invalid")
    current = current_time.astimezone(UTC).isoformat()
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT work_kind, work_key, status, attempts, created_at, updated_at, "
                "next_attempt_at FROM work WHERE work_kind = ? "
                "AND status IN ('pending','retry') AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, created_at, work_key LIMIT 1",
                (_WORK_KIND, current),
            ).fetchone()
            if row is None:
                return None
            item = store._work_from_row(row)
            result = db.execute(
                "UPDATE work SET status = 'running', attempts = attempts + 1, "
                "updated_at = ?, next_attempt_at = NULL "
                "WHERE work_kind = ? AND work_key = ? AND status = ? AND attempts = ?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if result.rowcount != 1:
                raise LocalSyncWorkError("local synchronization work claim changed unexpectedly")
        return store._get_work(item.work_kind, item.work_key)
    except sqlite3.Error as exc:
        raise LocalSyncWorkError("local synchronization work could not be claimed") from exc
    except StateError as exc:
        raise LocalSyncWorkError("local synchronization work claim is invalid") from exc


def execute_claimed_local_sync_work(
    store: StateStore,
    item: WorkItem,
    source: Path,
    snapshot_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
    *,
    branch: str = "main",
) -> LocalSyncWorkResult:
    """Execute exactly one already-claimed Local-sync item and persist its outcome."""
    _validate_claim(store, item, target, branch)
    try:
        synchronization = synchronize_local_configuration(
            store,
            source,
            snapshot_root,
            workspace_root,
            target,
            token,
            branch=branch,
        )
    except LocalSyncError:
        try:
            failed = store.fail_work(item, transient=True)
        except StateError as exc:
            raise LocalSyncWorkError(
                "local synchronization retry state could not be recorded"
            ) from exc
        return LocalSyncWorkResult(failed, None)

    try:
        if synchronization.disposition in _SUCCESS_DISPOSITIONS:
            transitioned = store.complete_work(item)
        elif synchronization.disposition in _BLOCKING_DISPOSITIONS:
            transitioned = store.fail_work(item, transient=False)
        else:
            raise LocalSyncWorkError("local synchronization returned an unknown disposition")
    except StateError as exc:
        raise LocalSyncWorkError(
            "local synchronization work outcome could not be recorded"
        ) from exc
    return LocalSyncWorkResult(transitioned, synchronization)


def _validate_claim(store: StateStore, item: WorkItem, target: str, branch: str) -> None:
    if type(store) is not StateStore:
        raise LocalSyncWorkError("local synchronization work state store is invalid")
    if type(item) is not WorkItem:
        raise LocalSyncWorkError("local synchronization work evidence is invalid")
    if item.work_kind != _WORK_KIND or item.status != "running" or item.attempts < 1:
        raise LocalSyncWorkError("local synchronization work is not an eligible claimed item")
    if item.work_key != local_sync_work_key(target, branch):
        raise LocalSyncWorkError("local synchronization work identity does not match the target")
