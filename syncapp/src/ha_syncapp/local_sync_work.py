"""Durable work-state adapter for one already-claimed Local -> Repo B synchronization."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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
    payload = f"{target.casefold()}\0{branch}".encode("utf-8")
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
            raise LocalSyncWorkError("local synchronization retry state could not be recorded") from exc
        return LocalSyncWorkResult(failed, None)

    try:
        if synchronization.disposition in _SUCCESS_DISPOSITIONS:
            transitioned = store.complete_work(item)
        elif synchronization.disposition in _BLOCKING_DISPOSITIONS:
            transitioned = store.fail_work(item, transient=False)
        else:
            raise LocalSyncWorkError("local synchronization returned an unknown disposition")
    except StateError as exc:
        raise LocalSyncWorkError("local synchronization work outcome could not be recorded") from exc
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
