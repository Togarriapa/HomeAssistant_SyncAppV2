from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import pytest
from ha_syncapp.core_event_stream import CoreEventStreamError
from ha_syncapp.runtime_event_lifecycle import (
    RuntimeEventLifecycleError,
    run_runtime_event_lifecycle,
)
from ha_syncapp.runtime_sync_work import runtime_sync_work_key
from ha_syncapp.state import StateStore

Handler = Callable[[Mapping[str, object]], Awaitable[None]]
Ready = Callable[[], Awaitable[None]]


class _SequenceConsumer:
    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.ready_calls = 0
        self.event_calls = 0
        self.tokens: list[str | None] = []

    async def __call__(
        self,
        handler: Handler,
        *,
        on_ready: Ready | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        assert max_events is None
        self.tokens.append(token)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert on_ready is not None
        await on_ready()
        self.ready_calls += 1
        await handler({"event_type": "state_changed"})
        self.event_calls += 1
        assert type(outcome) is int
        return outcome


def test_reconnect_succeeds_after_transient_failures(tmp_path: Path) -> None:
    consumer = _SequenceConsumer(
        [
            CoreEventStreamError("first"),
            CoreEventStreamError("second"),
            1,
        ]
    )
    with StateStore(tmp_path) as store:
        result = asyncio.run(
            run_runtime_event_lifecycle(
                store,
                "Owner/Home",
                token="secret-token",
                max_attempts=3,
                initial_backoff_seconds=0.001,
                max_backoff_seconds=0.002,
                consumer=consumer,
            )
        )
        work = store.claim_work_kind("runtime")

    assert result.attempts == 3
    assert result.reconnects == 2
    assert result.stopped is False
    assert result.events_consumed == 1
    assert consumer.ready_calls == 1
    assert consumer.event_calls == 1
    assert consumer.tokens == ["secret-token", "secret-token", "secret-token"]
    assert work is not None
    assert work.work_key == runtime_sync_work_key("Owner/Home")


def test_attempt_exhaustion_is_bounded_and_sanitized(tmp_path: Path) -> None:
    consumer = _SequenceConsumer(
        [CoreEventStreamError("secret-one"), CoreEventStreamError("secret-two")]
    )
    with StateStore(tmp_path) as store:
        with pytest.raises(RuntimeEventLifecycleError) as error:
            asyncio.run(
                run_runtime_event_lifecycle(
                    store,
                    "Owner/Home",
                    token="secret-token",
                    max_attempts=2,
                    initial_backoff_seconds=0.001,
                    max_backoff_seconds=0.001,
                    consumer=consumer,
                )
            )

    assert str(error.value) == "runtime event reconnect attempts exhausted"
    assert "secret-one" not in str(error.value)
    assert "secret-two" not in str(error.value)
    assert "secret-token" not in str(error.value)
    assert len(consumer.tokens) == 2


def test_backoff_progression_is_exponential_and_capped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = _SequenceConsumer(
        [
            CoreEventStreamError("one"),
            CoreEventStreamError("two"),
            CoreEventStreamError("three"),
            0,
        ]
    )
    observed: list[float] = []

    async def fake_wait(stop: asyncio.Event, delay: float) -> bool:
        assert stop.is_set() is False
        observed.append(delay)
        return False

    monkeypatch.setattr("ha_syncapp.runtime_event_lifecycle._wait_for_stop", fake_wait)
    with StateStore(tmp_path) as store:
        result = asyncio.run(
            run_runtime_event_lifecycle(
                store,
                "Owner/Home",
                max_attempts=4,
                initial_backoff_seconds=1.0,
                max_backoff_seconds=2.0,
                consumer=consumer,
            )
        )

    assert observed == [1.0, 2.0, 2.0]
    assert result.attempts == 4
    assert result.reconnects == 3


def test_shutdown_during_backoff_stops_without_extra_attempt(tmp_path: Path) -> None:
    stop = asyncio.Event()

    class _StopConsumer(_SequenceConsumer):
        async def __call__(
            self,
            handler: Handler,
            *,
            on_ready: Ready | None = None,
            token: str | None = None,
            max_events: int | None = None,
        ) -> int:
            del handler, on_ready, token, max_events
            stop.set()
            raise CoreEventStreamError("disconnect")

    consumer = _StopConsumer([])
    with StateStore(tmp_path) as store:
        result = asyncio.run(
            run_runtime_event_lifecycle(
                store,
                "Owner/Home",
                stop_event=stop,
                max_attempts=5,
                consumer=consumer,
            )
        )
        work = store.claim_work_kind("runtime")

    assert result.attempts == 1
    assert result.reconnects == 0
    assert result.stopped is True
    assert work is None


def test_each_successful_reconnect_readiness_precedes_event(tmp_path: Path) -> None:
    order: list[str] = []
    attempts = 0

    async def consumer(
        handler: Handler,
        *,
        on_ready: Ready | None = None,
        token: str | None = None,
        max_events: int | None = None,
    ) -> int:
        nonlocal attempts
        del token, max_events
        attempts += 1
        if attempts == 1:
            raise CoreEventStreamError("disconnect-before-ready")
        assert on_ready is not None
        order.append("ready")
        await on_ready()
        order.append("event")
        await handler({"event_type": "state_changed"})
        return 1

    with StateStore(tmp_path) as store:
        result = asyncio.run(
            run_runtime_event_lifecycle(
                store,
                "Owner/Home",
                max_attempts=2,
                initial_backoff_seconds=0.001,
                max_backoff_seconds=0.001,
                consumer=consumer,
            )
        )

    assert result.attempts == 2
    assert order == ["ready", "event"]


def test_non_stream_failure_fails_closed_without_retry(tmp_path: Path) -> None:
    consumer = _SequenceConsumer([RuntimeError("secret-sentinel")])
    with StateStore(tmp_path) as store:
        with pytest.raises(RuntimeEventLifecycleError) as error:
            asyncio.run(
                run_runtime_event_lifecycle(
                    store,
                    "Owner/Home",
                    max_attempts=5,
                    consumer=consumer,
                )
            )

    assert str(error.value) == "runtime event lifecycle failed closed"
    assert "secret-sentinel" not in str(error.value)
    assert len(consumer.tokens) == 1
