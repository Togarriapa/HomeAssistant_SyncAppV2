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
from ha_syncapp.runtime_sync_retrigger import (
    RuntimeSyncRetriggerError,
    RuntimeSyncRetriggerResult,
    run_runtime_sync_retrigger_pass,
)
from ha_syncapp.state import StateStore


class RetriggerCycleError(RuntimeError):
    """The bounded Retrigger cycle could not complete safely."""


@dataclass(frozen=True, slots=True)
class RetriggerCycleResult:
    """Per-lane evidence from one bounded Retrigger cycle."""

    local_sync: LocalSyncRetriggerResult
    database_sync: DatabaseSyncRetriggerResult
    runtime_sync: RuntimeSyncRetriggerResult


def run_retrigger_cycle(
    store: StateStore,
    home_assistant_root: Path,
    snapshot_staging_root: Path,
    local_workspace_root: Path,
    recorder_database: Path,
    database_staging_root: Path,
    database_snapshot_root: Path,
    database_workspace_root: Path,
    runtime_staging_root: Path,
    runtime_snapshot_root: Path,
    runtime_workspace_root: Path,
    target: str,
    github_token: str,
    *,
    core_token: str | None = None,
) -> RetriggerCycleResult:
    """Process at most one item from each implemented outbound lane in order."""
    if type(store) is not StateStore:
        raise RetriggerCycleError("retrigger cycle state store is invalid")

    try:
        local_sync = run_local_sync_retrigger_pass(
            store,
            home_assistant_root,
            snapshot_staging_root,
            local_workspace_root,
            target,
            github_token,
        )
        database_sync = run_database_sync_retrigger_pass(
            store,
            recorder_database,
            database_staging_root,
            database_snapshot_root,
            database_workspace_root,
            target,
            github_token,
        )
        runtime_sync = run_runtime_sync_retrigger_pass(
            store,
            runtime_staging_root,
            runtime_snapshot_root,
            runtime_workspace_root,
            target,
            github_token,
            core_token=core_token,
        )
    except (
        LocalSyncRetriggerError,
        DatabaseSyncRetriggerError,
        RuntimeSyncRetriggerError,
    ) as exc:
        raise RetriggerCycleError("retrigger cycle failed closed") from exc

    return RetriggerCycleResult(
        local_sync=local_sync,
        database_sync=database_sync,
        runtime_sync=runtime_sync,
    )
