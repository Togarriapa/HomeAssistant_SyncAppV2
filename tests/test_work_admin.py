from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.state import StateError, StateStore, WorkItem
from ha_syncapp.work_admin import WorkAdministrationError, retry_blocked_work


def _open_store(tmp_path: Path) -> tuple[Path, StateStore]:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return data, store


def _block(
    store: StateStore,
    work_kind: str,
    work_key: str,
    *,
    created_at: datetime,
) -> WorkItem:
    store.enqueue_work(work_kind, work_key, now=created_at)
    claimed = store.claim_work_kind(work_kind, now=created_at)
    assert claimed is not None
    return store.fail_work(claimed, transient=False, now=created_at + timedelta(seconds=1))


def test_explicit_retry_resets_only_exact_blocked_item(tmp_path: Path) -> None:
    _, store = _open_store(tmp_path)
    created = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)
    first = _block(store, "candidate", "candidate-a", created_at=created)
    second = _block(
        store,
        "candidate",
        "candidate-b",
        created_at=created + timedelta(seconds=2),
    )
    retry_at = created + timedelta(minutes=5)
    try:
        retried = retry_blocked_work(
            store,
            first.work_kind,
            first.work_key,
            now=retry_at,
        )
        unchanged = store.enqueue_work(second.work_kind, second.work_key, now=retry_at)
    finally:
        store.__exit__(None, None, None)

    assert retried.status == "pending"
    assert retried.attempts == 0
    assert retried.created_at == first.created_at
    assert retried.updated_at == retry_at
    assert retried.next_attempt_at == retry_at
    assert unchanged == second


def test_explicit_retry_starts_a_fresh_retry_budget(tmp_path: Path) -> None:
    _, store = _open_store(tmp_path)
    created = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)
    blocked = _block(store, "candidate", "candidate-a", created_at=created)
    retry_at = created + timedelta(minutes=5)
    try:
        retry_blocked_work(store, blocked.work_kind, blocked.work_key, now=retry_at)
        claimed = store.claim_work_kind(blocked.work_kind, now=retry_at)
    finally:
        store.__exit__(None, None, None)

    assert claimed is not None
    assert claimed.work_key == blocked.work_key
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.created_at == blocked.created_at


@pytest.mark.parametrize("status", ["pending", "running", "retry", "succeeded"])
def test_explicit_retry_rejects_non_blocked_state(tmp_path: Path, status: str) -> None:
    _, store = _open_store(tmp_path)
    created = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)
    item = store.enqueue_work("candidate", f"candidate-{status}", now=created)
    if status != "pending":
        claimed = store.claim_work_kind("candidate", now=created)
        assert claimed is not None
        item = claimed
        if status == "retry":
            item = store.fail_work(claimed, transient=True, now=created + timedelta(seconds=1))
        elif status == "succeeded":
            item = store.complete_work(claimed, now=created + timedelta(seconds=1))
    try:
        with pytest.raises(
            WorkAdministrationError,
            match="only blocked work can be administratively retried",
        ):
            retry_blocked_work(store, item.work_kind, item.work_key, now=created + timedelta(minutes=1))
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("work_kind", "work_key"),
    [("Bad Kind", "candidate-a"), ("candidate", "bad\x00key")],
)
def test_explicit_retry_rejects_invalid_identity(
    tmp_path: Path,
    work_kind: str,
    work_key: str,
) -> None:
    _, store = _open_store(tmp_path)
    try:
        with pytest.raises(WorkAdministrationError, match="failed closed") as error:
            retry_blocked_work(store, work_kind, work_key)
    finally:
        store.__exit__(None, None, None)

    assert isinstance(error.value.__cause__, StateError)


def test_explicit_retry_rejects_missing_exact_item(tmp_path: Path) -> None:
    _, store = _open_store(tmp_path)
    try:
        with pytest.raises(
            WorkAdministrationError,
            match="administrative retry work item does not exist",
        ):
            retry_blocked_work(store, "candidate", "missing")
    finally:
        store.__exit__(None, None, None)


def test_explicit_retry_persists_across_restart(tmp_path: Path) -> None:
    data, store = _open_store(tmp_path)
    created = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)
    blocked = _block(store, "candidate", "candidate-a", created_at=created)
    retry_at = created + timedelta(minutes=5)
    retry_blocked_work(store, blocked.work_kind, blocked.work_key, now=retry_at)
    store.__exit__(None, None, None)

    with StateStore(data) as reopened:
        claimed = reopened.claim_work_kind(blocked.work_kind, now=retry_at)

    assert claimed is not None
    assert claimed.work_key == blocked.work_key
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.created_at == blocked.created_at
