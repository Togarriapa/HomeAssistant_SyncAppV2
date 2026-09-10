"""One bounded recovery pass for durable runtime inventory synchronization work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ha_syncapp.runtime_sync_process import RuntimeSyncProcessError, run_runtime_sync_process
from ha_syncapp.runtime_sync_work import RuntimeSyncWorkResult
from ha_syncapp.state import StateError, StateStore


class RuntimeSyncRetriggerError(RuntimeError):
    """A bounded runtime recovery pass could not complete safely."""


@dataclass(frozen=True, slots=True)
class RuntimeSyncRetriggerResult:
    """Sanitized outcome of one bounded runtime recovery pass."""

    recovered_interrupted: int
    processed: RuntimeSyncWorkResult | None


def run_runtime_sync_retrigger_pass(
    store: StateStore,
    runtime_staging_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    github_token: str,
    *,
    core_token: str | None = None,
) -> RuntimeSyncRetriggerResult:
    """Recover interrupted work and delegate one bounded normal processing pass."""
    if type(store) is not StateStore:
        raise RuntimeSyncRetriggerError("runtime synchronization retrigger state store is invalid")

    try:
        recovered = store.recover_interrupted_work()
        result = run_runtime_sync_process(
            store,
            runtime_staging_root,
            snapshot_staging_root,
            workspace_root,
            target,
            github_token,
            core_token=core_token,
        )
        return RuntimeSyncRetriggerResult(
            recovered_interrupted=recovered,
            processed=result.processed,
        )
    except (StateError, RuntimeSyncProcessError) as exc:
        raise RuntimeSyncRetriggerError(
            "runtime synchronization retrigger pass failed closed"
        ) from exc
