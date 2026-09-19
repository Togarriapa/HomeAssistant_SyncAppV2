from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.core_health_observation import (
    CoreHealthResponse,
    observe_core_api_once,
)
from ha_syncapp.core_health_window import (
    CoreHealthWindowError,
    advance_core_health_window_once,
    load_core_health_window,
)
from test_core_health_observation import _acknowledged

TOKEN = "window-supervisor-secret"
START = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
HEALTHY = CoreHealthResponse(200, "application/json", b'{"message":"API running."}')


def _initial_health(tmp_path: Path, monkeypatch):
    chain, authorization = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    observe_core_api_once(
        store,
        authorization.deployment_id,
        token=TOKEN,
        observed_at=START,
        transport=lambda *_args: HEALTHY,
    )
    return chain, authorization


def test_window_starts_durably_without_waiting_or_network(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _initial_health(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = advance_core_health_window_once(
            store,
            authorization.deployment_id,
            observation_seconds=300,
            now=START,
            transport=lambda *_args: pytest.fail("window start performed a request"),
        )
        assert result.status == "observing"
        assert result.replayed is False
        assert result.deadline_at == START + timedelta(seconds=300)
        window = load_core_health_window(store, authorization.deployment_id)
        assert window is not None
        assert window.completed_at is None
    finally:
        store.__exit__(None, None, None)


def test_before_deadline_is_credential_free_network_free_replay(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _initial_health(tmp_path, monkeypatch)
    store = chain[0]
    try:
        advance_core_health_window_once(
            store, authorization.deployment_id, observation_seconds=300, now=START
        )
        result = advance_core_health_window_once(
            store,
            authorization.deployment_id,
            observation_seconds=300,
            now=START + timedelta(seconds=299),
            transport=lambda *_args: pytest.fail("early window performed a request"),
        )
        assert result.status == "observing"
        assert result.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_deadline_requires_one_fresh_exact_health_proof_and_completed_replay(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _initial_health(tmp_path, monkeypatch)
    store = chain[0]
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        return HEALTHY

    try:
        advance_core_health_window_once(
            store, authorization.deployment_id, observation_seconds=300, now=START
        )
        completed = advance_core_health_window_once(
            store,
            authorization.deployment_id,
            observation_seconds=300,
            now=START + timedelta(seconds=300),
            token=TOKEN,
            transport=transport,
        )
        replay = advance_core_health_window_once(
            store,
            authorization.deployment_id,
            observation_seconds=300,
            now=START + timedelta(seconds=301),
            transport=lambda *_args: pytest.fail("completed replay performed a request"),
        )
        assert completed.status == "healthy"
        assert completed.replayed is False
        assert replay.status == "healthy"
        assert replay.replayed is True
        assert calls == 1
    finally:
        store.__exit__(None, None, None)


def test_unhealthy_deadline_probe_persists_no_completion_and_can_retry(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _initial_health(tmp_path, monkeypatch)
    store = chain[0]
    deadline = START + timedelta(seconds=300)
    try:
        advance_core_health_window_once(
            store, authorization.deployment_id, observation_seconds=300, now=START
        )
        with pytest.raises(CoreHealthWindowError, match="health is unavailable"):
            advance_core_health_window_once(
                store,
                authorization.deployment_id,
                observation_seconds=300,
                now=deadline,
                token=TOKEN,
                transport=lambda *_args: CoreHealthResponse(
                    503, "application/json", b'{"message":"API running."}'
                ),
            )
        assert load_core_health_window(store, authorization.deployment_id).completed_at is None
        retry = advance_core_health_window_once(
            store,
            authorization.deployment_id,
            observation_seconds=300,
            now=deadline + timedelta(seconds=1),
            token=TOKEN,
            transport=lambda *_args: HEALTHY,
        )
        assert retry.status == "healthy"
    finally:
        store.__exit__(None, None, None)


def test_missing_initial_health_never_starts_window(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CoreHealthWindowError, match="initial health is unavailable"):
            advance_core_health_window_once(
                store, authorization.deployment_id, observation_seconds=300, now=START
            )
        assert load_core_health_window(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("duration", [True, 29, 3601])
def test_invalid_duration_fails_before_state_or_network(
    tmp_path: Path, monkeypatch, duration: object
) -> None:
    chain, authorization = _initial_health(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CoreHealthWindowError, match="duration is invalid"):
            advance_core_health_window_once(
                store,
                authorization.deployment_id,
                observation_seconds=duration,
                now=START,
                transport=lambda *_args: pytest.fail("invalid duration performed a request"),
            )
        assert load_core_health_window(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_tampered_window_fails_closed(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _initial_health(tmp_path, monkeypatch)
    store = chain[0]
    try:
        advance_core_health_window_once(
            store, authorization.deployment_id, observation_seconds=300, now=START
        )
        store._connection.execute(
            "UPDATE core_health_window SET initial_health_sha256 = ?", ("f" * 64,)
        )
        store._connection.commit()
        with pytest.raises(CoreHealthWindowError, match="state is invalid"):
            load_core_health_window(store, authorization.deployment_id)
    finally:
        store.__exit__(None, None, None)
