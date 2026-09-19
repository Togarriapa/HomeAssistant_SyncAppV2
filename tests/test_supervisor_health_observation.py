from datetime import timedelta
from pathlib import Path

import pytest
from ha_syncapp.core_health_observation import CoreHealthResponse
from ha_syncapp.core_health_window import advance_core_health_window_once
from ha_syncapp.supervisor_health_observation import (
    SupervisorHealthError,
    SupervisorHealthResponse,
    load_supervisor_health_observation,
    observe_supervisor_health_once,
)
from test_core_health_window import START, TOKEN, _initial_health

SUPERVISOR_HEALTHY = SupervisorHealthResponse(
    200,
    "application/json; charset=utf-8",
    b'{"result":"ok","data":{"healthy":true,"supported":true,"version":"2026.09.0"}}',
)


def _completed_window(tmp_path: Path, monkeypatch):
    chain, authorization = _initial_health(tmp_path, monkeypatch)
    store = chain[0]
    advance_core_health_window_once(
        store, authorization.deployment_id, observation_seconds=300, now=START
    )
    advance_core_health_window_once(
        store,
        authorization.deployment_id,
        observation_seconds=300,
        now=START + timedelta(seconds=300),
        token=TOKEN,
        transport=lambda *_args: CoreHealthResponse(
            200, "application/json", b'{"message":"API running."}'
        ),
    )
    return chain, authorization


def test_records_one_exact_bounded_authenticated_supervisor_read(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    calls = []

    def transport(method, url, headers, timeout, limit):
        calls.append((method, url, headers, timeout, limit))
        return SUPERVISOR_HEALTHY

    try:
        result = observe_supervisor_health_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=301),
            timeout_seconds=7,
            max_response_bytes=4096,
            transport=transport,
        )
        assert result.status == "healthy"
        assert result.replayed is False
        assert calls == [
            (
                "GET",
                "http://supervisor/supervisor/info",
                {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"},
                7,
                4096,
            )
        ]
        observation = load_supervisor_health_observation(store, authorization.deployment_id)
        assert observation is not None
        assert observation.observed_at == START + timedelta(seconds=301)
    finally:
        store.__exit__(None, None, None)


def test_completed_replay_is_credential_and_network_free(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    try:
        observe_supervisor_health_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=301),
            transport=lambda *_args: SUPERVISOR_HEALTHY,
        )
        result = observe_supervisor_health_once(
            store,
            authorization.deployment_id,
            transport=lambda *_args: pytest.fail("replay performed a request"),
        )
        assert result.status == "healthy"
        assert result.replayed is True
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "response",
    [
        SupervisorHealthResponse(503, "application/json", b"{}"),
        SupervisorHealthResponse(200, "text/plain", b"{}"),
        SupervisorHealthResponse(200, "application/json", b"not-json"),
        SupervisorHealthResponse(
            200, "application/json", b'{"result":"error","data":{"healthy":true,"supported":true}}'
        ),
        SupervisorHealthResponse(
            200,
            "application/json",
            b'{"result":"ok","extra":1,"data":{"healthy":true,"supported":true}}',
        ),
        SupervisorHealthResponse(
            200, "application/json", b'{"result":"ok","data":{"healthy":false,"supported":true}}'
        ),
        SupervisorHealthResponse(
            200, "application/json", b'{"result":"ok","data":{"healthy":true,"supported":false}}'
        ),
        SupervisorHealthResponse(
            200, "application/json", b'{"result":"ok","data":{"healthy":1,"supported":true}}'
        ),
        SupervisorHealthResponse(
            200, "application/json", b'{"result":"ok","data":{"healthy":true}}'
        ),
        SupervisorHealthResponse(
            200,
            "application/json",
            b'{"result":"ok","result":"ok","data":{"healthy":true,"supported":true}}',
        ),
    ],
)
def test_invalid_or_unacceptable_response_persists_no_authority(
    tmp_path: Path, monkeypatch, response: SupervisorHealthResponse
) -> None:
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(SupervisorHealthError, match="health is unavailable"):
            observe_supervisor_health_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                transport=lambda *_args: response,
            )
        assert load_supervisor_health_observation(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_incomplete_core_window_prevents_request(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _initial_health(tmp_path, monkeypatch)
    store = chain[0]
    try:
        advance_core_health_window_once(
            store, authorization.deployment_id, observation_seconds=300, now=START
        )
        with pytest.raises(SupervisorHealthError, match="window is not complete"):
            observe_supervisor_health_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                transport=lambda *_args: pytest.fail("unauthorized request"),
            )
    finally:
        store.__exit__(None, None, None)


def test_oversized_response_and_invalid_limits_fail_without_authority(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(SupervisorHealthError, match="health is unavailable"):
            observe_supervisor_health_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                max_response_bytes=8,
                transport=lambda *_args: SUPERVISOR_HEALTHY,
            )
        with pytest.raises(SupervisorHealthError, match="limits are invalid"):
            observe_supervisor_health_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                timeout_seconds=True,
                transport=lambda *_args: pytest.fail("invalid limits performed a request"),
            )
    finally:
        store.__exit__(None, None, None)


def test_tampered_binding_fails_closed(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    try:
        observe_supervisor_health_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=301),
            transport=lambda *_args: SUPERVISOR_HEALTHY,
        )
        store._connection.execute(
            "UPDATE supervisor_health_observation SET core_window_sha256 = ?",
            ("f" * 64,),
        )
        store._connection.commit()
        with pytest.raises(SupervisorHealthError, match="state is invalid"):
            load_supervisor_health_observation(store, authorization.deployment_id)
    finally:
        store.__exit__(None, None, None)


def test_observation_cannot_predate_completed_window(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(SupervisorHealthError, match="state is invalid"):
            observe_supervisor_health_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                observed_at=START + timedelta(seconds=299),
                transport=lambda *_args: SUPERVISOR_HEALTHY,
            )
        assert load_supervisor_health_observation(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_persistence_failure_is_sanitized_and_records_no_authority(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    try:
        store._connection.execute(
            "CREATE TRIGGER reject_supervisor_health BEFORE INSERT ON "
            "supervisor_health_observation BEGIN SELECT RAISE(ABORT, 'secret detail'); END"
        )
        with pytest.raises(SupervisorHealthError, match="state is invalid") as error:
            observe_supervisor_health_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                observed_at=START + timedelta(seconds=301),
                transport=lambda *_args: SUPERVISOR_HEALTHY,
            )
        assert "secret detail" not in str(error.value)
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM supervisor_health_observation"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)
