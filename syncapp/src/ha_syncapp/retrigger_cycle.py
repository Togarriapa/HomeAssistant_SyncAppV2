"""One scheduler-neutral Retrigger cycle for currently implemented outbound lanes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ha_syncapp.database_sync_retrigger import (
    DatabaseSyncRetriggerError,
    DatabaseSyncRetriggerResult,
    run_database_sync_retrigger_pass,
)
from ha_syncapp.local_sync_retrigger import (
    LocalSyncRetriggerError,
    LocalSyncRetriggerResult,
    run_local_sync_retrigger_pass,
)
from ha_syncapp.state import StateStore


class RetriggerCycleError(RuntimeError):
    """The bounded Retrigger cycle could not complete safely."""


@dataclass(frozen=True, slots=True)
class RetriggerCycleResult:
    """Per-lane evidence from one bounded Retrigger cycle."""

    local_sync: LocalSyncRetriggerResult
    database_sync: DatabaseSyncRetriggerResult


def run_retrigger_cycle(
    store: StateStore,
    home_assistant_root: Path,
    snapshot_staging_root: Path,
    local_workspace_root: Path,
    recorder_database: Path,
    database_staging_root: Path,
    database_snapshot_root: Path,
    database_workspace_root: Path,
    target: str,
    token: str,
) -> RetriggerCycleResult:
    """Process at most one Local-sync and one database item in deterministic order."""
    if type(store) is not StateStore:
        raise RetriggerCycleError("retrigger cycle state store is invalid")

    try:
        local_sync = run_local_sync_retrigger_pass(
            store,
            home_assistant_root,
            snapshot_staging_root,
            local_workspace_root,
            target,
            token,
        )
        database_sync = run_database_sync_retrigger_pass(
            store,
            recorder_database,
            database_staging_root,
            database_snapshot_root,
            database_workspace_root,
            target,
            token,
        )
    except (LocalSyncRetriggerError, DatabaseSyncRetriggerError) as exc:
        raise RetriggerCycleError("retrigger cycle failed closed") from exc

    return RetriggerCycleResult(local_sync=local_sync, database_sync=database_sync)
