from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp.candidate_startup_error_observation_execution import (
    CandidateStartupErrorObservationExecutionError,
    execute_candidate_startup_error_observation_once,
)
from ha_syncapp.startup_error_observation import observe_startup_errors_once
from test_core_health_window import START, TOKEN
from test_integration_observation import _factory
from test_startup_error_observation import (
    StartupSession,
    _entry,
    _prepared,
    _startup_responses,
)


def _running(tmp_path, monkeypatch):
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    now = START + timedelta(seconds=303)
    store.enqueue_work("candidate_observe_startup_errors", authorization.deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_startup_errors", now=now)
    assert item is not None
    return chain, authorization, item, now


def test_clear_observation_atomically_schedules_resource_observation(
    tmp_path, monkeypatch
) -> None:
    chain, authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    session = StartupSession(_startup_responses([]))
    try:
        result = execute_candidate_startup_error_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(session),
            now=now,
        )
        assert result.action == "resource_observation_scheduled"
        assert result.outcome == "clear"
        assert result.replayed is False
        assert result.work.status == "succeeded"
        assert result.successor.work_kind == "candidate_observe_resources"
        assert result.successor.work_key == authorization.deployment_id
        assert result.successor.status == "pending"
        assert session.sent[-1] == {"id": 1, "type": "system_log/list"}
    finally:
        store.__exit__(None, None, None)


def test_significant_errors_atomically_schedule_finalization_only(
    tmp_path, monkeypatch
) -> None:
    chain, authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    session = StartupSession(
        _startup_responses(
            [_entry(level="ERROR", timestamp=(START + timedelta(seconds=302)).timestamp())]
        )
    )
    try:
        result = execute_candidate_startup_error_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(session),
            now=now,
        )
        assert result.action == "finalization_scheduled"
        assert result.outcome == "significant_errors"
        assert result.successor.work_kind == "candidate_finalize"
        assert result.successor.status == "pending"
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe_resources'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)


def test_failed_observation_replay_requires_no_credential_or_network(
    tmp_path, monkeypatch
) -> None:
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    now = START + timedelta(seconds=303)
    observe_startup_errors_once(
        store,
        authorization.deployment_id,
        token=TOKEN,
        observed_at=now,
        session_factory=_factory(
            StartupSession(
                _startup_responses(
                    [
                        _entry(
                            level="CRITICAL",
                            timestamp=(START + timedelta(seconds=302)).timestamp(),
                        )
                    ]
                )
            )
        ),
    )
    store.enqueue_work("candidate_observe_startup_errors", authorization.deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_startup_errors", now=now)
    assert item is not None
    try:
        result = execute_candidate_startup_error_observation_once(
            store,
            item,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
            now=now + timedelta(seconds=1),
        )
        assert result.replayed is True
        assert result.action == "finalization_scheduled"
    finally:
        store.__exit__(None, None, None)


def test_unavailable_session_is_transient_and_sanitized(tmp_path, monkeypatch) -> None:
    chain, _authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CandidateStartupErrorObservationExecutionError) as caught:
            execute_candidate_startup_error_observation_once(
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
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "responses",
    [
        None,
        [
            {"type": "auth_required"},
            {"type": "auth_invalid", "message": "private credential detail"},
        ],
    ],
)
def test_invalid_or_rejected_credential_is_deterministic(
    tmp_path, monkeypatch, responses
) -> None:
    chain, _authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    factory = (
        (lambda *_args: pytest.fail("invalid credential opened a session"))
        if responses is None
        else _factory(StartupSession(responses))
    )
    try:
        with pytest.raises(CandidateStartupErrorObservationExecutionError) as caught:
            execute_candidate_startup_error_observation_once(
                store,
                item,
                token=" invalid " if responses is None else TOKEN,
                session_factory=factory,
                now=now,
            )
        assert caught.value.transient is False
        assert "private" not in str(caught.value).lower()
    finally:
        store.__exit__(None, None, None)
