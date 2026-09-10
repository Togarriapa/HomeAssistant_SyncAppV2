"""Bounded normal processor for one durable runtime synchronization item."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .core_runtime_bundle import CoreRuntimeBundleError, collect_core_runtime_bundle
from .runtime_sync_work import (
    RuntimeSyncWorkError,
    RuntimeSyncWorkResult,
    claim_runtime_sync_work,
    execute_claimed_runtime_sync_work,
    runtime_sync_work_key,
)
from .state import StateError, StateStore


class RuntimeSyncProcessError(RuntimeError):
    """Normal runtime work processing failed closed."""


@dataclass(frozen=True, slots=True)
class RuntimeSyncProcessResult:
    """Sanitized result from processing at most one runtime item."""

    processed: RuntimeSyncWorkResult | None


def run_runtime_sync_process(
    store: StateStore,
    runtime_staging_root: Path,
    snapshot_staging_root: Path,
    workspace_root: Path,
    target: str,
    github_token: str,
    *,
    core_token: str | None = None,
) -> RuntimeSyncProcessResult:
    """Claim and process at most one runtime item without recovery semantics."""
    if type(store) is not StateStore:
        raise RuntimeSyncProcessError("runtime synchronization state store is invalid")

    try:
        item = claim_runtime_sync_work(store)
        if item is None:
            return RuntimeSyncProcessResult(processed=None)

        if item.work_key != runtime_sync_work_key(target):
            blocked = store.fail_work(item, transient=False)
            return RuntimeSyncProcessResult(
                processed=RuntimeSyncWorkResult(blocked, None),
            )

        try:
            inventory = collect_core_runtime_bundle(token=core_token)
        except CoreRuntimeBundleError:
            retry = store.fail_work(item, transient=True)
            return RuntimeSyncProcessResult(
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
        return RuntimeSyncProcessResult(processed=processed)
    except (StateError, RuntimeSyncWorkError) as exc:
        raise RuntimeSyncProcessError("runtime synchronization processing failed closed") from exc
