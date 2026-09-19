from __future__ import annotations

from pathlib import Path

import pytest
from ha_syncapp.core_health_observation import (
    CoreHealthError,
    CoreHealthResponse,
    load_core_health_observation,
    observe_core_api_once,
)
from ha_syncapp.core_restart_transport import CoreRestartResponse, request_core_restart_once
from test_core_restart_transport import TOKEN, _authorized


def _acknowledged(tmp_path: Path, monkeypatch):
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    request_core_restart_once(
        store,
        authorization,
        token=TOKEN,
        transport=lambda *_args: CoreRestartResponse(
            200, "application/json", b'{"result":"ok","data":{}}'
        ),
    )
    return chain, authorization


def test_exact_core_api_success_is_persisted_after_acknowledged_restart(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    calls = []

    def transport(method, url, headers, timeout_seconds, max_response_bytes):
        assert load_core_health_observation(store, authorization.deployment_id) is None
        calls.append((method, url, headers, timeout_seconds, max_response_bytes))
        return CoreHealthResponse(200, "application/json", b'{"message":"API running."}')

    try:
        result = observe_core_api_once(
            store, authorization.deployment_id, token=TOKEN, transport=transport
        )
        assert result.status == "healthy"
        assert result.replayed is False
        assert calls == [
            (
                "GET",
                "http://supervisor/core/api/",
                {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"},
                10.0,
                16 * 1024,
            )
        ]
        observation = load_core_health_observation(store, authorization.deployment_id)
        assert observation is not None
        assert observation.restart_attempt_sha256
    finally:
        store.__exit__(None, None, None)


def test_healthy_replay_is_network_and_credential_independent(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        return CoreHealthResponse(200, "application/json", b'{"message":"API running."}')

    try:
        observe_core_api_once(store, authorization.deployment_id, token=TOKEN, transport=transport)
        replay = observe_core_api_once(store, authorization.deployment_id, transport=transport)
        assert replay.status == "healthy"
        assert replay.replayed is True
        assert calls == 1
    finally:
        store.__exit__(None, None, None)


def test_unacknowledged_restart_never_reaches_health_transport(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CoreHealthError, match="restart is not acknowledged"):
            observe_core_api_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                transport=lambda *_args: pytest.fail("unacknowledged restart was probed"),
            )
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "response",
    [
        CoreHealthResponse(503, "application/json", b'{"message":"API running."}'),
        CoreHealthResponse(200, "text/plain", b'{"message":"API running."}'),
        CoreHealthResponse(200, "application/json", b"not-json"),
        CoreHealthResponse(200, "application/json", b'{"message":"wrong"}'),
        CoreHealthResponse(
            200,
            "application/json",
            b'{"message":"API running.","message":"API running."}',
        ),
        CoreHealthResponse(200, "application/json", b"x" * (16 * 1024 + 1)),
    ],
)
def test_unhealthy_response_persists_no_false_success(
    tmp_path: Path, monkeypatch, response
) -> None:
    chain, authorization = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CoreHealthError, match="health is unavailable"):
            observe_core_api_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                transport=lambda *_args: response,
            )
        assert load_core_health_observation(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_transport_failure_is_sanitized_and_retryable(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]

    def fail(*_args):
        raise TimeoutError("PRIVATE response detail")

    try:
        with pytest.raises(CoreHealthError, match="health is unavailable") as caught:
            observe_core_api_once(store, authorization.deployment_id, token=TOKEN, transport=fail)
        assert "PRIVATE" not in str(caught.value)
        assert load_core_health_observation(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_tampered_observation_fails_closed(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    try:
        observe_core_api_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            transport=lambda *_args: CoreHealthResponse(
                200, "application/json", b'{"message":"API running."}'
            ),
        )
        store._connection.execute(
            "UPDATE core_health_observation SET restart_attempt_sha256 = ?", ("f" * 64,)
        )
        store._connection.commit()
        with pytest.raises(CoreHealthError, match="state is invalid"):
            load_core_health_observation(store, authorization.deployment_id)
    finally:
        store.__exit__(None, None, None)
