from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ha_syncapp.runtime_event_session import RuntimeEventSession
from ha_syncapp.runtime_sync_work import runtime_sync_work_key
from ha_syncapp.state import StateStore


def test_ready_schedules_full_runtime_baseline(tmp_path: Path) -> None:
    scheduled_at = datetime(2026, 9, 10, 6, 30, tzinfo=UTC)
    with StateStore(tmp_path) as store:
        session = RuntimeEventSession(store, "Owner/Home")
        asyncio.run(session.ready(now=scheduled_at))
        item = store.claim_work_kind("runtime", now=scheduled_at)

    assert item is not None
    assert item.work_key == runtime_sync_work_key("Owner/Home")
    assert item.status == "running"


def test_event_after_completed_baseline_starts_fresh_generation(tmp_path: Path) -> None:
    baseline_at = datetime(2026, 9, 10, 6, 30, tzinfo=UTC)
    event_at = baseline_at + timedelta(seconds=5)
    with StateStore(tmp_path) as store:
        session = RuntimeEventSession(store, "Owner/Home")
        asyncio.run(session.ready(now=baseline_at))
        running = store.claim_work_kind("runtime", now=baseline_at)
        assert running is not None
        store.complete_work(running, now=baseline_at)

        asyncio.run(session.event({"event_type": "state_changed"}, now=event_at))
        item = store.claim_work_kind("runtime", now=event_at)

    assert item is not None
    assert item.status == "running"
    assert item.created_at == event_at


def test_ready_does_not_rearm_blocked_runtime_work(tmp_path: Path) -> None:
    first_at = datetime(2026, 9, 10, 6, 30, tzinfo=UTC)
    reconnect_at = first_at + timedelta(minutes=1)
    with StateStore(tmp_path) as store:
        session = RuntimeEventSession(store, "Owner/Home")
        asyncio.run(session.ready(now=first_at))
        running = store.claim_work_kind("runtime", now=first_at)
        assert running is not None
        blocked = store.fail_work(running, transient=False, now=first_at)
        assert blocked.status == "blocked"

        asyncio.run(session.ready(now=reconnect_at))
        repeated = store.claim_work_kind("runtime", now=reconnect_at)

    assert repeated is None
