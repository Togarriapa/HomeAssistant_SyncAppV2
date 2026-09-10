"""One normal runtime synchronization bootstrap for service startup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .runtime_sync_process import (
    RuntimeSyncProcessError,
    RuntimeSyncProcessResult,
    run_runtime_sync_process,
)
from .runtime_sync_schedule import RuntimeSyncScheduleError, schedule_runtime_sync_generation
from .state import StateStore, WorkItem


class RuntimeStartupError(RuntimeError):
    """The startup runtime bootstrap failed closed."""


@dataclass(frozen=True, slots=True)
class RuntimeStartupResult:
    """Sanitized outcome from one startup runtime bootstrap."""

    scheduled: WorkItem
    processed: RuntimeSyncProcessResult


def run_startup_runtime_sync(
    store: StateStore,
    runtime_staging_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    github_token: str,
    *,
    core_token: str | None = None,
) -> RuntimeStartupResult:
    """Schedule one normal runtime generation and process at most one eligible item."""
    if type(store) is not StateStore:
        raise RuntimeStartupError("startup runtime state store is invalid")

    try:
        scheduled = schedule_runtime_sync_generation(store, target)
        processed = run_runtime_sync_process(
            store,
            runtime_staging_root,
            snapshot_staging_root,
            workspace_root,
            target,
            github_token,
            core_token=core_token,
        )
    except (RuntimeSyncScheduleError, RuntimeSyncProcessError) as exc:
        raise RuntimeStartupError("startup runtime synchronization failed closed") from exc

    return RuntimeStartupResult(scheduled=scheduled, processed=processed)
