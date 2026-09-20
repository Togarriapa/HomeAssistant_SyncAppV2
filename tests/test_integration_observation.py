from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from ha_syncapp.integration_observation import (
    IntegrationObservationError,
    load_integration_observation,
    observe_integrations_once,
)
from ha_syncapp.supervisor_health_observation import observe_supervisor_health_once
from test_core_health_window import START, TOKEN
from test_supervisor_health_observation import SUPERVISOR_HEALTHY, _completed_window


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = iter(responses)
        self.sent: list[dict[str, object]] = []

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout: float | None = None) -> str:
        del timeout
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            return response
        return json.dumps(response)


def _factory(session: FakeSession, observed: dict[str, object] | None = None):
    @contextmanager
    def factory(url: str, timeout: float, limit: int) -> Iterator[FakeSession]:
        if observed is not None:
            observed.update(url=url, timeout=timeout, limit=limit)
        yield session

    return factory


def _responses(entries: list[object]) -> list[object]:
    return [
        {"type": "auth_required", "ha_version": "2026.9.3"},
        {"type": "auth_ok", "ha_version": "2026.9.3"},
        {"id": 1, "type": "result", "success": True, "result": entries},
    ]


def _authorized(tmp_path: Path, monkeypatch):
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    observe_supervisor_health_once(
        store,
        authorization.deployment_id,
        token=TOKEN,
        observed_at=START + timedelta(seconds=301),
        transport=lambda *_args: SUPERVISOR_HEALTHY,
    )
    return chain, authorization


def test_records_one_bounded_command_and_aggregate_only(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    session = FakeSession(
        _responses(
            [
                {"entry_id": "loaded-secret", "state": "loaded", "disabled_by": None},
                {"entry_id": "disabled-secret", "state": "not_loaded", "disabled_by": "user"},
            ]
        )
    )
    boundary: dict[str, object] = {}
    try:
        result = observe_integrations_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=302),
            timeout_seconds=7,
            max_message_bytes=8192,
            session_factory=_factory(session, boundary),
        )
        assert result.status == "initialized"
        assert result.replayed is False
        assert result.entry_count == 2
        assert result.disabled_count == 1
        assert boundary == {
            "url": "ws://supervisor/core/websocket",
            "timeout": 7,
            "limit": 8192,
        }
        assert session.sent == [
            {"access_token": TOKEN, "type": "auth"},
            {"id": 1, "type": "config_entries/get"},
        ]
        row = store._connection.execute("SELECT * FROM integration_observation").fetchone()
        assert row is not None
        assert "secret" not in repr(row)
    finally:
        store.__exit__(None, None, None)


def test_completed_replay_is_credential_and_network_free(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        observe_integrations_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=302),
            session_factory=_factory(FakeSession(_responses([]))),
        )
        result = observe_integrations_once(
            store,
            authorization.deployment_id,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
        )
        assert result.replayed is True
        assert result.entry_count == 0
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "entry",
    [
        {"entry_id": "one", "state": "setup_error", "disabled_by": None},
        {"entry_id": "one", "state": "setup_retry", "disabled_by": None},
        {"entry_id": "one", "state": "migration_error", "disabled_by": None},
        {"entry_id": "one", "state": "not_loaded", "disabled_by": None},
        {"entry_id": "one", "state": "unload_in_progress", "disabled_by": None},
        {"entry_id": "one", "state": True, "disabled_by": None},
        {"state": "loaded", "disabled_by": None},
        {"entry_id": "one", "state": "loaded"},
    ],
)
def test_enabled_uninitialized_or_malformed_entry_persists_no_authority(
    tmp_path: Path, monkeypatch, entry: dict[str, object]
) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(IntegrationObservationError, match="unavailable"):
            observe_integrations_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                session_factory=_factory(FakeSession(_responses([entry]))),
            )
        assert load_integration_observation(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "third",
    [
        {"id": 2, "type": "result", "success": True, "result": []},
        {"id": 1, "type": "result", "success": False, "error": {"message": "secret"}},
        {"id": 1, "type": "result", "success": True, "result": {}},
        {
            "id": 1,
            "type": "result",
            "success": True,
            "result": [
                {"entry_id": "same", "state": "loaded", "disabled_by": None},
                {"entry_id": "same", "state": "loaded", "disabled_by": None},
            ],
        },
        '{"id":1,"type":"result","success":true,"success":true,"result":[]}',
    ],
)
def test_invalid_protocol_is_sanitized(tmp_path: Path, monkeypatch, third: object) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(IntegrationObservationError, match="unavailable") as error:
            observe_integrations_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                session_factory=_factory(FakeSession(_responses([])[:2] + [third])),
            )
        assert "secret" not in str(error.value)
    finally:
        store.__exit__(None, None, None)


def test_missing_supervisor_proof_prevents_session(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(IntegrationObservationError, match="Supervisor health"):
            observe_integrations_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                session_factory=lambda *_args: pytest.fail("unauthorized session"),
            )
    finally:
        store.__exit__(None, None, None)


def test_tampering_fails_closed(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        observe_integrations_once(
            store,
            authorization.deployment_id,
            token=TOKEN,
            observed_at=START + timedelta(seconds=302),
            session_factory=_factory(FakeSession(_responses([]))),
        )
        store._connection.execute(
            "UPDATE integration_observation SET supervisor_health_sha256 = ?",
            ("f" * 64,),
        )
        store._connection.commit()
        with pytest.raises(IntegrationObservationError, match="state is invalid"):
            load_integration_observation(store, authorization.deployment_id)
    finally:
        store.__exit__(None, None, None)


def test_observation_cannot_predate_supervisor_proof(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(IntegrationObservationError, match="state is invalid"):
            observe_integrations_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                observed_at=START + timedelta(seconds=300),
                session_factory=_factory(FakeSession(_responses([]))),
            )
        assert load_integration_observation(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_persistence_failure_is_sanitized(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    store._connection.execute(
        "CREATE TRIGGER reject_integration_observation BEFORE INSERT ON "
        "integration_observation BEGIN SELECT RAISE(ABORT, 'secret-storage-detail'); END"
    )
    try:
        with pytest.raises(IntegrationObservationError, match="state is invalid") as error:
            observe_integrations_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                observed_at=START + timedelta(seconds=302),
                session_factory=_factory(FakeSession(_responses([]))),
            )
        assert "secret" not in str(error.value)
        assert load_integration_observation(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)


def test_oversized_message_is_rejected_without_persisting(tmp_path: Path, monkeypatch) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(IntegrationObservationError, match="unavailable"):
            observe_integrations_once(
                store,
                authorization.deployment_id,
                token=TOKEN,
                max_message_bytes=16,
                session_factory=_factory(FakeSession(["x" * 17])),
            )
        assert load_integration_observation(store, authorization.deployment_id) is None
    finally:
        store.__exit__(None, None, None)
