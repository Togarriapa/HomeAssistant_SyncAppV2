from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp.candidate_integration_observation_execution import (
    CandidateIntegrationObservationExecutionError,
    execute_candidate_integration_observation_once,
)
from ha_syncapp.integration_observation import observe_integrations_once
from test_core_health_window import START, TOKEN
from test_integration_observation import FakeSession, _authorized, _factory, _responses


def _running(tmp_path, monkeypatch):
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    now = START + timedelta(seconds=302)
    store.enqueue_work("candidate_observe_integrations", authorization.deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_integrations", now=now)
    assert item is not None
    return chain, authorization, item, now


def test_exact_observation_atomically_schedules_startup_error_observation(
    tmp_path, monkeypatch
) -> None:
    chain, authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    session = FakeSession(
        _responses(
            [
                {"entry_id": "loaded", "state": "loaded", "disabled_by": None},
                {"entry_id": "disabled", "state": "not_loaded", "disabled_by": "user"},
            ]
        )
    )
    try:
        result = execute_candidate_integration_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(session),
            now=now,
        )
        assert result.action == "startup_error_observation_scheduled"
        assert result.replayed is False
        assert result.entry_count == 2
        assert result.disabled_count == 1
        assert result.work.status == "succeeded"
        assert result.successor.work_kind == "candidate_observe_startup_errors"
        assert result.successor.work_key == authorization.deployment_id
        assert result.successor.status == "pending"
        assert session.sent == [
            {"access_token": TOKEN, "type": "auth"},
            {"id": 1, "type": "config_entries/get"},
        ]
    finally:
        store.__exit__(None, None, None)


def test_persisted_observation_replay_requires_no_credential_or_network(
    tmp_path, monkeypatch
) -> None:
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    now = START + timedelta(seconds=302)
    observe_integrations_once(
        store,
        authorization.deployment_id,
        token=TOKEN,
        observed_at=now,
        session_factory=_factory(FakeSession(_responses([]))),
    )
    store.enqueue_work("candidate_observe_integrations", authorization.deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_integrations", now=now)
    assert item is not None
    try:
        result = execute_candidate_integration_observation_once(
            store,
            item,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
            now=now + timedelta(seconds=1),
        )
        assert result.replayed is True
        assert result.successor.status == "pending"
    finally:
        store.__exit__(None, None, None)


def test_unavailable_session_is_transient_and_sanitized(tmp_path, monkeypatch) -> None:
    chain, _authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CandidateIntegrationObservationExecutionError) as caught:
            execute_candidate_integration_observation_once(
                store,
                item,
                token=TOKEN,
                session_factory=lambda *_args: (_ for _ in ()).throw(
                    TimeoutError("private transport detail")
                ),
                now=now,
            )
        assert caught.value.transient is True
        assert "private" not in str(caught.value).lower()
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe_startup_errors'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)


def test_invalid_credential_is_deterministic_and_network_free(tmp_path, monkeypatch) -> None:
    chain, _authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CandidateIntegrationObservationExecutionError) as caught:
            execute_candidate_integration_observation_once(
                store,
                item,
                token=" invalid ",
                session_factory=lambda *_args: pytest.fail("invalid credential opened a session"),
                now=now,
            )
        assert caught.value.transient is False
    finally:
        store.__exit__(None, None, None)


def test_rejected_credential_is_deterministic_and_sanitized(tmp_path, monkeypatch) -> None:
    chain, _authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    session = FakeSession(
        [
            {"type": "auth_required"},
            {"type": "auth_invalid", "message": "private credential detail"},
        ]
    )
    try:
        with pytest.raises(CandidateIntegrationObservationExecutionError) as caught:
            execute_candidate_integration_observation_once(
                store,
                item,
                token=TOKEN,
                session_factory=_factory(session),
                now=now,
            )
        assert caught.value.transient is False
        assert "private" not in str(caught.value).lower()
    finally:
        store.__exit__(None, None, None)


def test_tampered_supervisor_proof_is_deterministic_and_network_free(tmp_path, monkeypatch) -> None:
    chain, _authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    store._connection.execute(
        "UPDATE supervisor_health_observation SET core_window_sha256 = ?",
        ("f" * 64,),
    )
    store._connection.commit()
    try:
        with pytest.raises(CandidateIntegrationObservationExecutionError) as caught:
            execute_candidate_integration_observation_once(
                store,
                item,
                token=TOKEN,
                session_factory=lambda *_args: pytest.fail("tampered authority opened a session"),
                now=now,
            )
        assert caught.value.transient is False
    finally:
        store.__exit__(None, None, None)
