"""Durable work-state adapter for exact-artifact Repo B logs publication."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ha_syncapp.log_artifact_loader import LogArtifactLoadError, load_log_artifact
from ha_syncapp.log_sync import (
    LogSyncDisposition,
    LogSyncError,
    LogSyncResult,
    synchronize_log_artifact,
)
from ha_syncapp.state import StateError, StateStore, WorkItem

_WORK_KIND = "logs"
_ARTIFACT_ID = re.compile(r"^[0-9a-f]{64}$")
_TARGET_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_BLOCKING_DISPOSITIONS = {
    LogSyncDisposition.BASELINE_REQUIRED,
    LogSyncDisposition.DIVERGED,
    LogSyncDisposition.REMOTE_MISSING,
}
_SUCCESS_DISPOSITIONS = {
    LogSyncDisposition.INITIALIZED,
    LogSyncDisposition.PUBLISHED,
    LogSyncDisposition.NO_CHANGE,
}


class LogSyncWorkError(RuntimeError):
    """A durable logs-sync item cannot be executed or transitioned safely."""


@dataclass(frozen=True, slots=True)
class LogSyncWorkResult:
    """Persisted work outcome plus guarded logs synchronization evidence."""

    work: WorkItem
    synchronization: LogSyncResult | None


def log_sync_work_key(target: str, artifact_id: str) -> str:
    """Bind one durable work item to an exact Repo B target and log artifact ID."""
    _validate_target(target)
    if type(artifact_id) is not str or _ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise LogSyncWorkError("logs synchronization artifact identity is invalid")
    target_digest = hashlib.sha256(target.casefold().encode()).hexdigest()
    return f"{target_digest}:{artifact_id}"


def log_sync_artifact_id(target: str, work_key: str) -> str:
    """Recover the exact artifact ID only after proving the work key's target binding."""
    _validate_target(target)
    if type(work_key) is not str:
        raise LogSyncWorkError("logs synchronization work identity is invalid")
    parts = work_key.split(":")
    if (
        len(parts) != 2
        or _TARGET_DIGEST.fullmatch(parts[0]) is None
        or _ARTIFACT_ID.fullmatch(parts[1]) is None
        or work_key != log_sync_work_key(target, parts[1])
    ):
        raise LogSyncWorkError("logs synchronization work identity does not match the target")
    return parts[1]


def enqueue_log_sync_work(
    store: StateStore,
    artifact_root: Path,
    target: str,
    artifact_id: str,
) -> WorkItem:
    """Idempotently enqueue one exact artifact after reverifying it under the protected root."""
    if type(store) is not StateStore:
        raise LogSyncWorkError("logs synchronization work state store is invalid")
    try:
        load_log_artifact(artifact_root, artifact_id)
        return store.enqueue_work(_WORK_KIND, log_sync_work_key(target, artifact_id))
    except LogArtifactLoadError as exc:
        raise LogSyncWorkError(
            "logs synchronization artifact could not be enqueued safely"
        ) from exc
    except StateError as exc:
        raise LogSyncWorkError("logs synchronization work could not be enqueued") from exc


def claim_log_sync_work(
    store: StateStore,
    *,
    now: datetime | None = None,
) -> WorkItem | None:
    """Atomically claim only the oldest eligible logs synchronization item."""
    if type(store) is not StateStore:
        raise LogSyncWorkError("logs synchronization work state store is invalid")
    try:
        return store.claim_work_kind(_WORK_KIND, now=now)
    except StateError as exc:
        raise LogSyncWorkError("logs synchronization work claim is invalid") from exc


def execute_claimed_log_sync_work(
    store: StateStore,
    item: WorkItem,
    artifact_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
) -> LogSyncWorkResult:
    """Reconstruct, reverify, and publish the exact artifact bound to one claimed item."""
    artifact_id = _validate_claim(store, item, target)
    try:
        artifact = load_log_artifact(artifact_root, artifact_id)
    except LogArtifactLoadError:
        return _record_deterministic_failure(store, item)

    try:
        synchronization = synchronize_log_artifact(
            store,
            artifact,
            snapshot_staging_root,
            workspace_root,
            target,
            token,
        )
    except LogSyncError:
        try:
            failed = store.fail_work(item, transient=True)
        except StateError as exc:
            raise LogSyncWorkError(
                "logs synchronization retry state could not be recorded"
            ) from exc
        return LogSyncWorkResult(failed, None)

    try:
        if synchronization.disposition in _SUCCESS_DISPOSITIONS:
            transitioned = store.complete_work(item)
        elif synchronization.disposition in _BLOCKING_DISPOSITIONS:
            transitioned = store.fail_work(item, transient=False)
        else:
            raise LogSyncWorkError("logs synchronization returned an unknown disposition")
    except StateError as exc:
        raise LogSyncWorkError("logs synchronization work outcome could not be recorded") from exc
    return LogSyncWorkResult(transitioned, synchronization)


def _record_deterministic_failure(store: StateStore, item: WorkItem) -> LogSyncWorkResult:
    try:
        blocked = store.fail_work(item, transient=False)
    except StateError as exc:
        raise LogSyncWorkError("logs synchronization blocked state could not be recorded") from exc
    return LogSyncWorkResult(blocked, None)


def _validate_claim(store: StateStore, item: WorkItem, target: str) -> str:
    if type(store) is not StateStore:
        raise LogSyncWorkError("logs synchronization work state store is invalid")
    if type(item) is not WorkItem:
        raise LogSyncWorkError("logs synchronization work evidence is invalid")
    if item.work_kind != _WORK_KIND or item.status != "running" or item.attempts < 1:
        raise LogSyncWorkError("logs synchronization work is not an eligible claimed item")
    return log_sync_artifact_id(target, item.work_key)


def _validate_target(target: str) -> None:
    if not isinstance(target, str) or not target or target != target.strip():
        raise LogSyncWorkError("logs synchronization target identity is invalid")
