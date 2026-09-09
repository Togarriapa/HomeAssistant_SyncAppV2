"""One bounded recovery pass for durable runtime inventory synchronization work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ha_syncapp.core_runtime_bundle import CoreRuntimeBundleError, collect_core_runtime_bundle
from ha_syncapp.runtime_sync_work import (
    RuntimeSyncWorkError,
    RuntimeSyncWorkResult,
    claim_runtime_sync_work,
    execute_claimed_runtime_sync_work,
    runtime_sync_work_key,
)
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
    """Recover interrupted work and process at most one eligible runtime item."""
    if type(store) is not StateStore:
        raise RuntimeSyncRetriggerError("runtime synchronization retrigger state store is invalid")

    try:
        recovered = store.recover_interrupted_work()
        item = claim_runtime_sync_work(store)
        if item is None:
            return RuntimeSyncRetriggerResult(recovered_interrupted=recovered, processed=None)

        if item.work_key != runtime_sync_work_key(target):
            blocked = store.fail_work(item, transient=False)
            return RuntimeSyncRetriggerResult(
                recovered_interrupted=recovered,
                processed=RuntimeSyncWorkResult(blocked, None),
            )

        try:
            inventory = collect_core_runtime_bundle(token=core_token)
        except CoreRuntimeBundleError:
            retry = store.fail_work(item, transient=True)
            return RuntimeSyncRetriggerResult(
                recovered_interrupted=recovered,
                processed=RuntimeSyncWorkResult(retry, None),
            )

        processed = execute_claimed_runtime_sync_work(
            store,
            item,
            inventory,
            runtime_staging_root,
            snapshot_staging_root,
            workspace_root,
            target,
            github_token,
        )
        return RuntimeSyncRetriggerResult(recovered_interrupted=recovered, processed=processed)
    except (StateError, RuntimeSyncWorkError) as exc:
        raise RuntimeSyncRetriggerError(
            "runtime synchronization retrigger pass failed closed"
        ) from exc
