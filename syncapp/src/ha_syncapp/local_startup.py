"""One normal Local -> Repo B synchronization bootstrap for service startup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .local_sync_process import (
    LocalSyncProcessError,
    LocalSyncProcessResult,
    run_local_sync_process,
)
from .local_sync_schedule import LocalSyncScheduleError, schedule_local_sync_generation
from .state import StateStore, WorkItem


class LocalStartupError(RuntimeError):
    """The startup Local synchronization bootstrap failed closed."""


@dataclass(frozen=True, slots=True)
class LocalStartupResult:
    """Sanitized outcome from one startup Local synchronization bootstrap."""

    scheduled: WorkItem
    processed: LocalSyncProcessResult


def run_startup_local_sync(
    store: StateStore,
    source: Path,
    snapshot_root: Path,
    workspace_root: Path,
    target: str,
    github_token: str,
    *,
    branch: str = "main",
) -> LocalStartupResult:
    """Schedule one normal Local generation and process at most one eligible item."""
    if type(store) is not StateStore:
        raise LocalStartupError("startup Local synchronization state store is invalid")

    try:
        scheduled = schedule_local_sync_generation(store, target, branch=branch)
        processed = run_local_sync_process(
            store,
            source,
            snapshot_root,
            workspace_root,
            target,
            github_token,
            branch=branch,
        )
    except (LocalSyncScheduleError, LocalSyncProcessError) as exc:
        raise LocalStartupError("startup Local synchronization failed closed") from exc

    return LocalStartupResult(scheduled=scheduled, processed=processed)
