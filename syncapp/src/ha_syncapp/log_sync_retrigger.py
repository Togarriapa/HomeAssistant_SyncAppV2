"""One bounded recovery pass for exact-artifact Repo B logs synchronization work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ha_syncapp.log_sync_work import (
    LogSyncWorkError,
    LogSyncWorkResult,
    claim_log_sync_work,
    execute_claimed_log_sync_work,
    log_sync_artifact_id,
)
from ha_syncapp.state import StateError, StateStore


class LogSyncRetriggerError(RuntimeError):
    """A bounded logs recovery pass could not complete safely."""


@dataclass(frozen=True, slots=True)
class LogSyncRetriggerResult:
    """Sanitized outcome of one bounded logs recovery pass."""

    recovered_interrupted: int
    processed: LogSyncWorkResult | None


def run_log_sync_retrigger_pass(
    store: StateStore,
    artifact_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
) -> LogSyncRetriggerResult:
    """Recover interrupted work and process at most one eligible exact log artifact."""
    if type(store) is not StateStore:
        raise LogSyncRetriggerError("logs synchronization retrigger state store is invalid")

    try:
        recovered = store.recover_interrupted_work()
        item = claim_log_sync_work(store)
        if item is None:
            return LogSyncRetriggerResult(recovered_interrupted=recovered, processed=None)
        try:
            log_sync_artifact_id(target, item.work_key)
        except LogSyncWorkError:
            blocked = store.fail_work(item, transient=False)
            return LogSyncRetriggerResult(
                recovered_interrupted=recovered,
                processed=LogSyncWorkResult(blocked, None),
            )
        processed = execute_claimed_log_sync_work(
            store,
            item,
            artifact_root,
            snapshot_staging_root,
            workspace_root,
            target,
            token,
        )
        return LogSyncRetriggerResult(recovered_interrupted=recovered, processed=processed)
    except (StateError, LogSyncWorkError) as exc:
        raise LogSyncRetriggerError("logs synchronization retrigger pass failed closed") from exc
