from __future__ import annotations

import json
from pathlib import Path

import pytest
from ha_syncapp import core_restart_transport
from ha_syncapp.core_restart_transport import (
    CoreRestartError,
    CoreRestartResponse,
    load_core_restart_attempt,
    request_core_restart_once,
)
from ha_syncapp.live_apply_writer import apply_live_operation
from ha_syncapp.post_apply_activation import authorize_post_apply_activation
from test_post_apply_activation import _chain

TOKEN = "secret-supervisor-token"


def _authorized(tmp_path: Path, monkeypatch):
    chain = _chain(tmp_path, monkeypatch)
    store = chain[0]
    apply_live_operation(store, *chain[1:], operation_index=0)
    result = authorize_post_apply_activation(store, *chain[1:])
    assert result.authorization is not None
    return chain, result.authorization


def test_restart_is_journaled_before_exact_supervisor_request(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    calls = []

    def transport(method, url, headers, body, timeout_seconds, max_response_bytes):
        attempt = load_core_restart_attempt(store, authorization.deployment_id)
        assert attempt is not None
        assert attempt.phase == "request_started"
        calls.append((method, url, headers, body, timeout_seconds, max_response_bytes))
        return CoreRestartResponse(200, "application/json", b'{"result":"ok","data":{}}')

    try:
        result = request_core_restart_once(store, authorization, token=TOKEN, transport=transport)
        assert result.status == "request_acknowledged"
        assert result.replayed is False
        assert calls == [
            (
                "POST",
                "http://supervisor/core/restart",
                {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                },
                json.dumps({"force": False, "safe_mode": False}, separators=(",", ":")).encode(),
                60.0,
                64 * 1024,
            )
        ]
        assert load_core_restart_attempt(store, authorization.deployment_id).phase == (
            "request_acknowledged"
        )
    finally:
        store.__exit__(None, None, None)


def test_acknowledged_replay_never_restarts_again(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        return CoreRestartResponse(200, "application/json", b'{"result":"ok","data":{}}')

    try:
        request_core_restart_once(store, authorization, token=TOKEN, transport=transport)
        replay = request_core_restart_once(store, authorization, transport=transport)
        assert replay.status == "request_acknowledged"
        assert replay.replayed is True
        assert calls == 1
    finally:
        store.__exit__(None, None, None)


def test_uncertain_started_attempt_never_blindly_restarts(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]

    def fail(*_args):
        raise TimeoutError("SECRET transport failure")

    try:
        with pytest.raises(CoreRestartError, match="outcome is uncertain") as caught:
            request_core_restart_once(store, authorization, token=TOKEN, transport=fail)
        assert "SECRET" not in str(caught.value)
        assert load_core_restart_attempt(store, authorization.deployment_id).phase == (
            "request_started"
        )

        def forbidden(*_args):
            pytest.fail("uncertain restart was sent twice")

        retry = request_core_restart_once(store, authorization, transport=forbidden)
        assert retry.status == "reconciliation_required"
        assert retry.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_crash_after_response_remains_uncertain_without_duplicate_request(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        return CoreRestartResponse(200, "application/json", b'{"result":"ok","data":{}}')

    def interrupt(*_args, **_kwargs):
        raise SystemExit("simulated interruption")

    monkeypatch.setattr(core_restart_transport, "_record_acknowledged", interrupt)
    try:
        with pytest.raises(SystemExit):
            request_core_restart_once(store, authorization, token=TOKEN, transport=transport)
        retry = request_core_restart_once(store, authorization, token=TOKEN, transport=transport)
        assert retry.status == "reconciliation_required"
        assert calls == 1
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "response",
    [
        CoreRestartResponse(500, "application/json", b'{"result":"error"}'),
        CoreRestartResponse(200, "text/plain", b"ok"),
        CoreRestartResponse(200, "application/json", b"not-json"),
        CoreRestartResponse(200, "application/json", b'{"result":"error"}'),
        CoreRestartResponse(
            200,
            "application/json",
            b'{"result":"ok","result":"ok","data":{}}',
        ),
        CoreRestartResponse(200, "application/json", b"x" * (64 * 1024 + 1)),
    ],
)
def test_invalid_response_remains_uncertain(tmp_path: Path, monkeypatch, response) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CoreRestartError, match="outcome is uncertain"):
            request_core_restart_once(
                store, authorization, token=TOKEN, transport=lambda *_args: response
            )
        assert load_core_restart_attempt(store, authorization.deployment_id).phase == (
            "request_started"
        )
    finally:
        store.__exit__(None, None, None)


def test_forged_authorization_and_invalid_token_fail_before_journal(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    forged = object.__new__(type(authorization))
    for name in authorization.__slots__:
        object.__setattr__(forged, name, getattr(authorization, name))
    object.__setattr__(forged, "candidate_sha", "f" * 40)
    try:
        with pytest.raises(CoreRestartError, match="authorization is invalid"):
            request_core_restart_once(store, forged, token=TOKEN, transport=lambda *_args: None)
        with pytest.raises(CoreRestartError, match="credential is invalid"):
            request_core_restart_once(
                store, authorization, token=" bad\n", transport=lambda *_args: None
            )
        assert load_core_restart_attempt(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_invalid_limits_fail_before_journal(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CoreRestartError, match="limits are invalid"):
            request_core_restart_once(
                store,
                authorization,
                token=TOKEN,
                timeout_seconds=61,
                transport=lambda *_args: None,
            )
        assert load_core_restart_attempt(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_tampered_journal_fails_closed_without_request(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]

    def fail(*_args):
        raise TimeoutError("uncertain")

    try:
        with pytest.raises(CoreRestartError, match="outcome is uncertain"):
            request_core_restart_once(store, authorization, token=TOKEN, transport=fail)
        store._connection.execute(
            "UPDATE core_restart_attempt SET authorization_record_sha256 = ?",
            ("f" * 64,),
        )
        store._connection.commit()

        with pytest.raises(CoreRestartError, match="state is invalid"):
            request_core_restart_once(
                store,
                authorization,
                token=TOKEN,
                transport=lambda *_args: pytest.fail("tampered journal reached transport"),
            )
    finally:
        store.__exit__(None, None, None)
