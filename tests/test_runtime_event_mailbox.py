from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from ha_syncapp.runtime_event_mailbox import (
    RuntimeEventMailbox,
    RuntimeEventMailboxError,
    RuntimeEventSignal,
    RuntimeEventSignalKind,
    drain_runtime_event_mailbox,
)
from ha_syncapp.runtime_sync_work import runtime_sync_work_key
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    store = StateStore(data)
    store.__enter__()
    return store


def test_ready_and_event_signals_schedule_one_coalesced_runtime_item(tmp_path: Path) -> None:
    mailbox = RuntimeEventMailbox()
    asyncio.run(mailbox.ready())
    asyncio.run(mailbox.event({"event_type": "state_changed"}))
    store = _store(tmp_path)
    try:
        result = drain_runtime_event_mailbox(mailbox, store, TARGET)
        claimed = store.claim_work_kind("runtime")
        second = store.claim_work_kind("runtime")
    finally:
        store.__exit__(None, None, None)

    assert result.consumed == 2
    assert result.ready_signals == 1
    assert result.event_signals == 1
    assert claimed is not None
    assert claimed.work_key == runtime_sync_work_key(TARGET)
    assert second is None


def test_mailbox_capacity_exhaustion_fails_closed() -> None:
    mailbox = RuntimeEventMailbox(capacity=1)
    asyncio.run(mailbox.ready())

    with pytest.raises(RuntimeEventMailboxError, match="capacity exhausted"):
        asyncio.run(mailbox.event({"event_type": "state_changed"}))


def test_event_callback_rejects_non_normalized_or_unknown_evidence() -> None:
    mailbox = RuntimeEventMailbox()

    with pytest.raises(RuntimeEventMailboxError, match="evidence is invalid"):
        asyncio.run(mailbox.event({"event_type": "state_changed", "data": {}}))
    with pytest.raises(RuntimeEventMailboxError, match="evidence is invalid"):
        asyncio.run(mailbox.event({"event_type": "not_subscribed"}))


def test_drain_limit_leaves_later_signals_for_next_owner_pass(tmp_path: Path) -> None:
    mailbox = RuntimeEventMailbox()
    asyncio.run(mailbox.ready())
    asyncio.run(mailbox.event({"event_type": "state_changed"}))
    store = _store(tmp_path)
    try:
        first = drain_runtime_event_mailbox(mailbox, store, TARGET, max_signals=1)
        second = drain_runtime_event_mailbox(mailbox, store, TARGET, max_signals=1)
        empty = drain_runtime_event_mailbox(mailbox, store, TARGET, max_signals=1)
    finally:
        store.__exit__(None, None, None)

    assert first.consumed == 1
    assert first.ready_signals == 1
    assert first.event_signals == 0
    assert second.consumed == 1
    assert second.ready_signals == 0
    assert second.event_signals == 1
    assert empty.consumed == 0


def test_public_signal_boundary_rejects_tampered_signal() -> None:
    mailbox = RuntimeEventMailbox()

    with pytest.raises(RuntimeEventMailboxError, match="signal is invalid"):
        mailbox.put(RuntimeEventSignal(RuntimeEventSignalKind.READY, "state_changed"))


def test_blocked_runtime_work_is_not_rearmed_by_mailbox(tmp_path: Path) -> None:
    store = _store(tmp_path)
    key = runtime_sync_work_key(TARGET)
    store.enqueue_work("runtime", key)
    claimed = store.claim_work_kind("runtime")
    assert claimed is not None
    blocked = store.fail_work(claimed, transient=False)
    assert blocked.status == "blocked"
    mailbox = RuntimeEventMailbox()
    asyncio.run(mailbox.ready())
    asyncio.run(mailbox.event({"event_type": "state_changed"}))

    try:
        result = drain_runtime_event_mailbox(mailbox, store, TARGET)
        eligible = store.claim_work_kind("runtime")
    finally:
        store.__exit__(None, None, None)

    assert result.consumed == 2
    assert eligible is None


def test_invalid_limits_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeEventMailboxError, match="capacity is invalid"):
        RuntimeEventMailbox(capacity=0)

    mailbox = RuntimeEventMailbox()
    store = _store(tmp_path)
    try:
        with pytest.raises(RuntimeEventMailboxError, match="drain limit is invalid"):
            drain_runtime_event_mailbox(mailbox, store, TARGET, max_signals=0)
    finally:
        store.__exit__(None, None, None)
