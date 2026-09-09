"""One bounded recovery pass for durable Local -> Repo B synchronization work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ha_syncapp.local_sync_work import (
    LocalSyncWorkError,
    LocalSyncWorkResult,
    claim_local_sync_work,
    execute_claimed_local_sync_work,
)
from ha_syncapp.state import StateError, StateStore


class LocalSyncRetriggerError(RuntimeError):
    """A bounded Local-sync recovery pass could not complete safely."""


@dataclass(frozen=True, slots=True)
class LocalSyncRetriggerResult:
    """Sanitized outcome of one bounded Local-sync recovery pass."""

    recovered_interrupted: int
    processed: LocalSyncWorkResult | None


def run_local_sync_retrigger_pass(
    store: StateStore,
    source: Path,
    snapshot_root: Path,
    workspace_root: Path,
    target: str,
    token: str,
    *,
    branch: str = "main",
) -> LocalSyncRetriggerResult:
    """Recover interrupted work and process at most one eligible Local-sync item."""
    if type(store) is not StateStore:
        raise LocalSyncRetriggerError("local synchronization retrigger state store is invalid")

    try:
        recovered = store.recover_interrupted_work()
        item = claim_local_sync_work(store)
        if item is None:
            return LocalSyncRetriggerResult(recovered_interrupted=recovered, processed=None)
        processed = execute_claimed_local_sync_work(
            store,
            item,
            source,
            snapshot_root,
            workspace_root,
            target,
            token,
            branch=branch,
        )
        return LocalSyncRetriggerResult(recovered_interrupted=recovered, processed=processed)
    except (StateError, LocalSyncWorkError) as exc:
        raise LocalSyncRetriggerError("local synchronization retrigger pass failed closed") from exc
