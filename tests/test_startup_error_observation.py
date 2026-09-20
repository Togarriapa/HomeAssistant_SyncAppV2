from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.core_restart_transport import load_core_restart_attempt
from ha_syncapp.integration_observation import observe_integrations_once
from ha_syncapp.startup_error_observation import (
    StartupErrorObservationError,
    load_startup_error_observation,
    observe_startup_errors_once,
)
from test_core_health_window import START, TOKEN
from test_integration_observation import FakeSession, _authorized, _factory, _responses


class StartupSession(FakeSession):
    pass


def _startup_responses(entries: list[object]) -> list[object]:
    return [
        {"type": "auth_required", "ha_version": "2026.9.3"},
        {"type": "auth_ok", "ha_version": "2026.9.3"},
        {"id": 1, "type": "result", "success": True, "result": entries},
    ]


def _entry(
    *,
    level: object = "WARNING",
    timestamp: object,
    first_occurred: object | None = None,
    count: object = 1,
) -> dict[str, object]:
    return {
        "name": "secret.logger",
        "message": ["secret message"],
        "level": level,
        "source": ["secret.py", 12],
        "timestamp": timestamp,
        "exception": "secret exception",
        "count": count,
        "first_occurred": timestamp if first_occurred is None else first_occurred,
    }


def _prepared(tmp_path: Path, monkeypatch):
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    observe_integrations_once(
        store,
        authorization.deployment_id,
        token=TOKEN,
        observed_at=START + timedelta(seconds=302),
        session_factory=_factory(FakeSession(_responses([]))),
    )
    return chain, authorization


def test_records_one_bounded_command_and_content_free_aggregate(
    tmp_path: Path, monkeypatch
) -> None:
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    restart = load_core_restart_attempt(store, authorization.deployment_id)
    assert restart is not None
    session = StartupSession(
        _startup_responses(
            [
                _entry(
                    level="ERROR",
                    timestamp=(restart.updated_at - timedelta(seconds=1)).timestamp(),
                ),
                _entry(level="WARNING", timestamp=(START + timedelta(seconds=302)).timestamp()),
            ]
        )
    )
    boundary: dict[str, object] = {}
    try:
        result = observe_startup_errors_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=303),
            timeout_seconds=7,
            max_message_bytes=8192,
            session_factory=_factory(session, boundary),
        )
        assert result.status == "clear"
        assert result.replayed is False
        assert result.inspected_count == 2
        assert result.warning_count == 1
        assert result.significant_error_count == 0
        assert boundary == {
            "url": "ws://supervisor/core/websocket",
            "timeout": 7,
            "limit": 8192,
        }
        assert session.sent == [
            {"access_token": TOKEN, "type": "auth"},
            {"id": 1, "type": "system_log/list"},
        ]
        row = store._connection.execute("SELECT * FROM startup_error_observation").fetchone()
        assert row is not None
        assert "secret" not in repr(row)
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("level", ["ERROR", "CRITICAL"])
def test_significant_error_is_durable_deterministic_failure(
    tmp_path: Path, monkeypatch, level: str
) -> None:
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = observe_startup_errors_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=303),
            session_factory=_factory(
                StartupSession(
                    _startup_responses(
                        [_entry(level=level, timestamp=(START + timedelta(seconds=302)).timestamp())]
                    )
                )
            ),
        )
        assert result.status == "significant_errors"
        assert result.significant_error_count == 1
        replay = observe_startup_errors_once(
            store,
            authorization.deployment_id,
            session_factory=lambda *_args: pytest.fail("failure replay opened a session"),
        )
        assert replay.status == "significant_errors"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_clear_replay_is_credential_and_network_free(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    try:
        observe_startup_errors_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=303),
            session_factory=_factory(StartupSession(_startup_responses([]))),
        )
        replay = observe_startup_errors_once(
            store,
            authorization.deployment_id,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
        )
        assert replay.status == "clear"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "entry",
    [
        _entry(level="INFO", timestamp=(START + timedelta(seconds=302)).timestamp()),
        _entry(level=True, timestamp=(START + timedelta(seconds=302)).timestamp()),
        _entry(timestamp=True),
        _entry(timestamp=(START + timedelta(seconds=304)).timestamp()),
        _entry(
            timestamp=(START + timedelta(seconds=302)).timestamp(),
            first_occurred=(START + timedelta(seconds=303)).timestamp(),
        ),
        _entry(timestamp=(START + timedelta(seconds=302)).timestamp(), count=0),
        {"level": "ERROR", "timestamp": (START + timedelta(seconds=302)).timestamp()},
    ],
)
def test_malformed_or_ambiguous_entry_persists_no_evidence(
    tmp_path: Path, monkeypatch, entry: dict[str, object]
) -> None:
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(StartupErrorObservationError, match="unavailable"):
            observe_startup_errors_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                observed_at=START + timedelta(seconds=303),
                session_factory=_factory(StartupSession(_startup_responses([entry]))),
            )
        assert load_startup_error_observation(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "third",
    [
        {"id": 2, "type": "result", "success": True, "result": []},
        {"id": 1, "type": "result", "success": False, "error": {"message": "secret"}},
        {"id": 1, "type": "result", "success": True, "result": {}},
        '{"id":1,"type":"result","success":true,"success":true,"result":[]}',
    ],
)
def test_invalid_protocol_is_sanitized(tmp_path: Path, monkeypatch, third: object) -> None:
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(StartupErrorObservationError, match="unavailable") as error:
            observe_startup_errors_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                observed_at=START + timedelta(seconds=303),
                session_factory=_factory(
                    StartupSession(_startup_responses([])[:2] + [third])
                ),
            )
        assert "secret" not in str(error.value)
    finally:
        store.__exit__(None, None, None)


def test_missing_integration_proof_prevents_session(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(StartupErrorObservationError, match="Integration initialization"):
            observe_startup_errors_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                session_factory=lambda *_args: pytest.fail("unauthorized session"),
            )
    finally:
        store.__exit__(None, None, None)


def test_tampering_fails_closed(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    try:
        observe_startup_errors_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=303),
            session_factory=_factory(StartupSession(_startup_responses([]))),
        )
        store._connection.execute(
            "UPDATE startup_error_observation SET integration_observation_sha256 = ?",
            ("f" * 64,),
        )
        store._connection.commit()
        with pytest.raises(StartupErrorObservationError, match="state is invalid"):
            load_startup_error_observation(store, authorization.deployment_id)
    finally:
        store.__exit__(None, None, None)


def test_persistence_failure_is_sanitized(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    store._connection.execute(
        "CREATE TRIGGER reject_startup_observation BEFORE INSERT ON "
        "startup_error_observation BEGIN SELECT RAISE(ABORT, 'secret-storage-detail'); END"
    )
    try:
        with pytest.raises(StartupErrorObservationError, match="state is invalid") as error:
            observe_startup_errors_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                observed_at=START + timedelta(seconds=303),
                session_factory=_factory(StartupSession(_startup_responses([]))),
            )
        assert "secret" not in str(error.value)
    finally:
        store.__exit__(None, None, None)
