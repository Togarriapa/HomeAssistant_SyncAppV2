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
from ha_syncapp.log_sync_retrigger import (
    LogSyncRetriggerError,
    LogSyncRetriggerResult,
    run_log_sync_retrigger_pass,
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
    log_sync: LogSyncRetriggerResult


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
    log_artifact_root: Path | None = None,
    log_snapshot_root: Path | None = None,
    log_workspace_root: Path | None = None,
) -> RetriggerCycleResult:
    """Process at most one item from each implemented outbound lane in order."""
    if type(store) is not StateStore:
        raise RetriggerCycleError("retrigger cycle state store is invalid")
    log_roots = (log_artifact_root, log_snapshot_root, log_workspace_root)
    if any(root is not None for root in log_roots) and not all(root is not None for root in log_roots):
        raise RetriggerCycleError("retrigger logs work roots are incomplete")

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
        if log_artifact_root is None:
            log_sync = LogSyncRetriggerResult(recovered_interrupted=0, processed=None)
        else:
            assert log_snapshot_root is not None
            assert log_workspace_root is not None
            log_sync = run_log_sync_retrigger_pass(
                store,
                log_artifact_root,
                log_snapshot_root,
                log_workspace_root,
                target,
                github_token,
            )
    except (
        LocalSyncRetriggerError,
        DatabaseSyncRetriggerError,
        RuntimeSyncRetriggerError,
        LogSyncRetriggerError,
    ) as exc:
        raise RetriggerCycleError("retrigger cycle failed closed") from exc

    return RetriggerCycleResult(
        local_sync=local_sync,
        database_sync=database_sync,
        runtime_sync=runtime_sync,
        log_sync=log_sync,
    )
