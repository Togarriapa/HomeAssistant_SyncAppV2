"""Bounded normal processor for one durable exact-artifact logs synchronization item."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .log_sync_work import (
    LogSyncWorkError,
    LogSyncWorkResult,
    claim_log_sync_work,
    execute_claimed_log_sync_work,
    log_sync_artifact_id,
)
from .state import StateError, StateStore


class LogSyncProcessError(RuntimeError):
    """Normal logs synchronization processing failed closed."""


@dataclass(frozen=True, slots=True)
class LogSyncProcessResult:
    """Sanitized result from processing at most one exact log artifact."""

    processed: LogSyncWorkResult | None


def run_log_sync_process(
    store: StateStore,
    artifact_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
) -> LogSyncProcessResult:
    """Claim and process at most one log artifact without recovery semantics."""
    if type(store) is not StateStore:
        raise LogSyncProcessError("logs synchronization state store is invalid")

    try:
        item = claim_log_sync_work(store)
        if item is None:
            return LogSyncProcessResult(processed=None)

        try:
            log_sync_artifact_id(target, item.work_key)
        except LogSyncWorkError:
            blocked = store.fail_work(item, transient=False)
            return LogSyncProcessResult(processed=LogSyncWorkResult(blocked, None))

        processed = execute_claimed_log_sync_work(
            store,
            item,
            artifact_root,
            snapshot_staging_root,
            workspace_root,
            target,
            token,
        )
        return LogSyncProcessResult(processed=processed)
    except (StateError, LogSyncWorkError) as exc:
        raise LogSyncProcessError("logs synchronization processing failed closed") from exc
