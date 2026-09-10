from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import pytest
from ha_syncapp.runtime_event_bridge import RuntimeEventBridge, RuntimeEventBridgeError
from ha_syncapp.runtime_event_worker import RuntimeEventWorkerReason
from ha_syncapp.runtime_sync_process import RuntimeSyncProcessError, RuntimeSyncProcessResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
EventHandler = Callable[[Mapping[str, object]], Awaitable[None]]
ReadyHandler = Callable[[], Awaitable[None]]


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _processor(calls: list[str]) -> Callable[..., RuntimeSyncProcessResult]:
    def process(
        store: StateStore,
        runtime_staging_root: Path,
        snapshot_staging_root: Path,
        workspace_root: Path,
        target: str,
        github_token: str,
    ) -> RuntimeSyncProcessResult:
        del store, runtime_staging_root, snapshot_staging_root, workspace_root, github_token
        calls.append(target)
        return RuntimeSyncProcessResult(processed=None)

    return process


def _bridge(
    tmp_path: Path,
    store: StateStore,
    consumer: Callable[..., Awaitable[int]],
    processor: Callable[..., RuntimeSyncProcessResult],
) -> RuntimeEventBridge:
    return RuntimeEventBridge(
        store,
        tmp_path / "runtime-staging",
        tmp_path / "runtime-snapshots",
        tmp_path / "runtime-workspaces",
        TARGET,
        "github-token",
        core_token="core-token",
        consumer=consumer,
        processor=processor,
    )


def test_ready_signal_is_drained_and_runtime_processing_runs_on_owner(tmp_path: Path) -> None:
    ready = threading.Event()
    calls: list[str] = []

    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        del handler, max_events
        assert token == "core-token"
        assert on_ready is not None
        await on_ready()
        ready.set()
        await asyncio.Event().wait()
        return 0

    store = _store(tmp_path)
    bridge = _bridge(tmp_path, store, consumer, _processor(calls))
    try:
        bridge.start()
        assert ready.wait(timeout=1)
        result = bridge.tick()
        stopped = bridge.stop(timeout_seconds=2)
    finally:
        store.__exit__(None, None, None)

    assert result.mailbox.consumed == 1
    assert result.mailbox.ready_signals == 1
    assert result.mailbox.event_signals == 0
    assert calls == [TARGET]
    assert stopped.reason is RuntimeEventWorkerReason.STOPPED


def test_event_signal_is_coalesced_into_existing_runtime_work(tmp_path: Path) -> None:
    forwarded = threading.Event()
    calls: list[str] = []

    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        del on_ready, token, max_events
        await handler({"event_type": "state_changed"})
        forwarded.set()
        await asyncio.Event().wait()
        return 0

    store = _store(tmp_path)
    bridge = _bridge(tmp_path, store, consumer, _processor(calls))
    try:
        bridge.start()
        assert forwarded.wait(timeout=1)
        first = bridge.tick()
        second = bridge.tick()
        bridge.stop(timeout_seconds=2)
    finally:
        store.__exit__(None, None, None)

    assert first.mailbox.event_signals == 1
    assert second.mailbox.consumed == 0
    assert calls == [TARGET, TARGET]


def test_idle_tick_still_runs_bounded_processor_for_eligible_retry(tmp_path: Path) -> None:
    connected = threading.Event()
    calls: list[str] = []

    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        del handler, on_ready, token, max_events
        connected.set()
        await asyncio.Event().wait()
        return 0

    store = _store(tmp_path)
    bridge = _bridge(tmp_path, store, consumer, _processor(calls))
    try:
        bridge.start()
        assert connected.wait(timeout=1)
        result = bridge.tick()
        bridge.stop(timeout_seconds=2)
    finally:
        store.__exit__(None, None, None)

    assert result.mailbox.consumed == 0
    assert calls == [TARGET]


def test_tick_from_non_owner_thread_is_rejected(tmp_path: Path) -> None:
    connected = threading.Event()
    errors: list[BaseException] = []

    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        del handler, on_ready, token, max_events
        connected.set()
        await asyncio.Event().wait()
        return 0

    store = _store(tmp_path)
    bridge = _bridge(tmp_path, store, consumer, _processor([]))
    try:
        bridge.start()
        assert connected.wait(timeout=1)

        def call_tick() -> None:
            try:
                bridge.tick()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=call_tick)
        thread.start()
        thread.join(timeout=1)
        bridge.stop(timeout_seconds=2)
    finally:
        store.__exit__(None, None, None)

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeEventBridgeError)
    assert "owner thread" in str(errors[0])


def test_unexpected_worker_completion_fails_closed(tmp_path: Path) -> None:
    completed = threading.Event()

    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        del handler, on_ready, token, max_events
        completed.set()
        return 0

    store = _store(tmp_path)
    bridge = _bridge(tmp_path, store, consumer, _processor([]))
    try:
        bridge.start()
        assert completed.wait(timeout=1)
        deadline = time.monotonic() + 1
        while True:
            try:
                bridge.tick()
            except RuntimeEventBridgeError as exc:
                assert "terminated unexpectedly" in str(exc)
                break
            if time.monotonic() >= deadline:
                pytest.fail("runtime event worker terminal state was not observed")
            time.sleep(0.01)
    finally:
        store.__exit__(None, None, None)


def test_processor_failure_is_sanitized_and_fail_closed(tmp_path: Path) -> None:
    connected = threading.Event()

    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        del handler, on_ready, token, max_events
        connected.set()
        await asyncio.Event().wait()
        return 0

    def failing_processor(*args: object) -> RuntimeSyncProcessResult:
        del args
        raise RuntimeSyncProcessError("secret diagnostic")

    store = _store(tmp_path)
    bridge = _bridge(tmp_path, store, consumer, failing_processor)
    try:
        bridge.start()
        assert connected.wait(timeout=1)
        with pytest.raises(RuntimeEventBridgeError, match="tick failed closed") as captured:
            bridge.tick()
        assert "secret diagnostic" not in str(captured.value)
        bridge.stop(timeout_seconds=2)
    finally:
        store.__exit__(None, None, None)


def test_lifecycle_rejects_tick_before_start_and_second_start(tmp_path: Path) -> None:
    async def consumer(
        handler: EventHandler,
        *,
        on_ready: ReadyHandler | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        del handler, on_ready, token, max_events
        await asyncio.Event().wait()
        return 0

    store = _store(tmp_path)
    bridge = _bridge(tmp_path, store, consumer, _processor([]))
    try:
        with pytest.raises(RuntimeEventBridgeError, match="lifecycle is invalid"):
            bridge.tick()
        bridge.start()
        with pytest.raises(RuntimeEventBridgeError, match="lifecycle is invalid"):
            bridge.start()
        bridge.stop(timeout_seconds=2)
    finally:
        store.__exit__(None, None, None)
