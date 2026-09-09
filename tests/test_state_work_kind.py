from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.state import StateError, StateStore


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_claim_work_kind_skips_earlier_other_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    store.enqueue_work("candidate", "candidate-a", now=start)
    store.enqueue_work("local_sync", "local-a", now=start + timedelta(seconds=1))

    try:
        claimed = store.claim_work_kind("local_sync", now=start + timedelta(seconds=1))
        unrelated = store.claim_work(now=start + timedelta(seconds=1))
    finally:
        store.__exit__(None, None, None)

    assert claimed is not None
    assert claimed.work_kind == "local_sync"
    assert claimed.work_key == "local-a"
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert unrelated is not None
    assert unrelated.work_kind == "candidate"


def test_claim_work_kind_preserves_oldest_ready_order_within_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    store.enqueue_work("database", "second", now=start + timedelta(seconds=1))
    store.enqueue_work("database", "first", now=start)

    try:
        first = store.claim_work_kind("database", now=start + timedelta(seconds=1))
        second = store.claim_work_kind("database", now=start + timedelta(seconds=1))
    finally:
        store.__exit__(None, None, None)

    assert first is not None and first.work_key == "first"
    assert second is not None and second.work_key == "second"


def test_claim_work_kind_uses_work_key_as_stable_tie_breaker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    store.enqueue_work("runtime", "z-last", now=start)
    store.enqueue_work("runtime", "a-first", now=start)

    try:
        first = store.claim_work_kind("runtime", now=start)
        second = store.claim_work_kind("runtime", now=start)
    finally:
        store.__exit__(None, None, None)

    assert first is not None and first.work_key == "a-first"
    assert second is not None and second.work_key == "z-last"


def test_claim_work_kind_respects_retry_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    store.enqueue_work("logs", "logs-a", now=start)
    item = store.claim_work_kind("logs", now=start)
    assert item is not None
    retry = store.fail_work(item, transient=True, now=start)
    assert retry.next_attempt_at == start + timedelta(seconds=60)

    try:
        before = store.claim_work_kind("logs", now=start + timedelta(seconds=59))
        ready = store.claim_work_kind("logs", now=start + timedelta(seconds=60))
    finally:
        store.__exit__(None, None, None)

    assert before is None
    assert ready is not None
    assert ready.status == "running"
    assert ready.attempts == 2


def test_claim_work_kind_returns_none_when_kind_has_no_ready_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue_work("candidate", "candidate-a")
    try:
        assert store.claim_work_kind("database") is None
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "work_kind",
    ["", "Local", "bad space", ".local", "a" * 65],
)
def test_claim_work_kind_rejects_invalid_kind(tmp_path: Path, work_kind: str) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(StateError, match="Invalid work kind"):
            store.claim_work_kind(work_kind)
    finally:
        store.__exit__(None, None, None)


def test_global_claim_order_remains_cross_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    store.enqueue_work("candidate", "candidate-a", now=start)
    store.enqueue_work("local_sync", "local-a", now=start + timedelta(seconds=1))

    try:
        claimed = store.claim_work(now=start + timedelta(seconds=1))
    finally:
        store.__exit__(None, None, None)

    assert claimed is not None
    assert claimed.work_kind == "candidate"
