"""Bounded normal processor for one durable Local -> Repo B synchronization item."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .local_sync_work import (
    LocalSyncWorkError,
    LocalSyncWorkResult,
    claim_local_sync_work,
    execute_claimed_local_sync_work,
    local_sync_work_key,
)
from .state import StateError, StateStore


class LocalSyncProcessError(RuntimeError):
    """Normal Local synchronization processing failed closed."""


@dataclass(frozen=True, slots=True)
class LocalSyncProcessResult:
    """Sanitized result from processing at most one Local item."""

    processed: LocalSyncWorkResult | None


def run_local_sync_process(
    store: StateStore,
    source: Path,
    snapshot_root: Path,
    workspace_root: Path,
    target: str,
    github_token: str,
    *,
    branch: str = "main",
) -> LocalSyncProcessResult:
    """Claim and process at most one Local item without recovery semantics."""
    if type(store) is not StateStore:
        raise LocalSyncProcessError("local synchronization state store is invalid")

    try:
        item = claim_local_sync_work(store)
        if item is None:
            return LocalSyncProcessResult(processed=None)

        if item.work_key != local_sync_work_key(target, branch):
            blocked = store.fail_work(item, transient=False)
            return LocalSyncProcessResult(processed=LocalSyncWorkResult(blocked, None))

        processed = execute_claimed_local_sync_work(
            store,
            item,
            source,
            snapshot_root,
            workspace_root,
            target,
            github_token,
            branch=branch,
        )
        return LocalSyncProcessResult(processed=processed)
    except (StateError, LocalSyncWorkError) as exc:
        raise LocalSyncProcessError("local synchronization processing failed closed") from exc
