"""Durable work-state adapter for guarded runtime inventory publication."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ha_syncapp.runtime_inventory import RuntimeInventoryInput
from ha_syncapp.runtime_sync import (
    RuntimeSyncDisposition,
    RuntimeSyncError,
    RuntimeSyncResult,
    synchronize_runtime_inventory,
)
from ha_syncapp.state import StateError, StateStore, WorkItem

_WORK_KIND = "runtime"
_BLOCKING_DISPOSITIONS = {
    RuntimeSyncDisposition.BASELINE_REQUIRED,
    RuntimeSyncDisposition.DIVERGED,
    RuntimeSyncDisposition.REMOTE_MISSING,
}
_SUCCESS_DISPOSITIONS = {
    RuntimeSyncDisposition.INITIALIZED,
    RuntimeSyncDisposition.PUBLISHED,
    RuntimeSyncDisposition.NO_CHANGE,
}


class RuntimeSyncWorkError(RuntimeError):
    """A durable runtime-sync item cannot be executed or transitioned safely."""


@dataclass(frozen=True, slots=True)
class RuntimeSyncWorkResult:
    """Persisted work outcome plus guarded runtime synchronization evidence."""

    work: WorkItem
    synchronization: RuntimeSyncResult | None


def runtime_sync_work_key(target: str) -> str:
    """Return a deterministic identity for the Repo B runtime publication lane."""
    if not isinstance(target, str) or not target or target != target.strip():
        raise RuntimeSyncWorkError("runtime synchronization work identity is invalid")
    payload = f"{target.casefold()}\0runtime".encode()
    return hashlib.sha256(payload).hexdigest()


def enqueue_runtime_sync_work(store: StateStore, target: str) -> WorkItem:
    """Idempotently enqueue one guarded runtime publication unit."""
    if type(store) is not StateStore:
        raise RuntimeSyncWorkError("runtime synchronization work state store is invalid")
    try:
        return store.enqueue_work(_WORK_KIND, runtime_sync_work_key(target))
    except StateError as exc:
        raise RuntimeSyncWorkError("runtime synchronization work could not be enqueued") from exc


def claim_runtime_sync_work(
    store: StateStore,
    *,
    now: datetime | None = None,
) -> WorkItem | None:
    """Atomically claim only the oldest eligible runtime synchronization item."""
    if type(store) is not StateStore:
        raise RuntimeSyncWorkError("runtime synchronization work state store is invalid")
    try:
        return store.claim_work_kind(_WORK_KIND, now=now)
    except StateError as exc:
        raise RuntimeSyncWorkError("runtime synchronization work claim is invalid") from exc


def execute_claimed_runtime_sync_work(
    store: StateStore,
    item: WorkItem,
    inventory: RuntimeInventoryInput,
    runtime_staging_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
) -> RuntimeSyncWorkResult:
    """Execute one already-claimed runtime item and durably record its outcome."""
    _validate_claim(store, item, inventory, target)
    try:
        synchronization = synchronize_runtime_inventory(
            store,
            inventory,
            runtime_staging_root,
            snapshot_staging_root,
            workspace_root,
            target,
            token,
        )
    except RuntimeSyncError:
        try:
            failed = store.fail_work(item, transient=True)
        except StateError as exc:
            raise RuntimeSyncWorkError(
                "runtime synchronization retry state could not be recorded"
            ) from exc
        return RuntimeSyncWorkResult(failed, None)

    try:
        if synchronization.disposition in _SUCCESS_DISPOSITIONS:
            transitioned = store.complete_work(item)
        elif synchronization.disposition in _BLOCKING_DISPOSITIONS:
            transitioned = store.fail_work(item, transient=False)
        else:
            raise RuntimeSyncWorkError("runtime synchronization returned an unknown disposition")
    except StateError as exc:
        raise RuntimeSyncWorkError(
            "runtime synchronization work outcome could not be recorded"
        ) from exc
    return RuntimeSyncWorkResult(transitioned, synchronization)


def _validate_claim(
    store: StateStore,
    item: WorkItem,
    inventory: RuntimeInventoryInput,
    target: str,
) -> None:
    if type(store) is not StateStore:
        raise RuntimeSyncWorkError("runtime synchronization work state store is invalid")
    if type(item) is not WorkItem:
        raise RuntimeSyncWorkError("runtime synchronization work evidence is invalid")
    if type(inventory) is not RuntimeInventoryInput:
        raise RuntimeSyncWorkError("runtime synchronization inventory evidence is invalid")
    if item.work_kind != _WORK_KIND or item.status != "running" or item.attempts < 1:
        raise RuntimeSyncWorkError("runtime synchronization work is not an eligible claimed item")
    if item.work_key != runtime_sync_work_key(target):
        raise RuntimeSyncWorkError("runtime synchronization work identity does not match the target")
