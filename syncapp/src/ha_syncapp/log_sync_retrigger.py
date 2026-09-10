"""One bounded recovery pass for exact-artifact Repo B logs synchronization work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .log_sync_process import LogSyncProcessError, run_log_sync_process
from .log_sync_work import LogSyncWorkResult
from .state import StateError, StateStore


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
    """Recover interrupted work, then delegate one normal logs processing attempt."""
    if type(store) is not StateStore:
        raise LogSyncRetriggerError("logs synchronization retrigger state store is invalid")

    try:
        recovered = store.recover_interrupted_work()
        processed = run_log_sync_process(
            store,
            artifact_root,
            snapshot_staging_root,
            workspace_root,
            target,
            token,
        )
        return LogSyncRetriggerResult(
            recovered_interrupted=recovered,
            processed=processed.processed,
        )
    except (StateError, LogSyncProcessError) as exc:
        raise LogSyncRetriggerError("logs synchronization retrigger pass failed closed") from exc
