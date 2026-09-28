from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp.candidate_supervisor_observation_execution import (
    CandidateSupervisorObservationExecutionError,
    execute_candidate_supervisor_observation_once,
)
from ha_syncapp.supervisor_health_observation import observe_supervisor_health_once
from test_core_health_window import START, TOKEN
from test_supervisor_health_observation import SUPERVISOR_HEALTHY, _completed_window


def _running(tmp_path, monkeypatch):
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    now = START + timedelta(seconds=301)
    store.enqueue_work("candidate_observe_supervisor", authorization.deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_supervisor", now=now)
    assert item is not None
    return chain, authorization, item, now


def test_exact_health_proof_atomically_schedules_integration_observation(
    tmp_path, monkeypatch
) -> None:
    chain, authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        return SUPERVISOR_HEALTHY

    try:
        result = execute_candidate_supervisor_observation_once(
            store,
            item,
            token=TOKEN,
            transport=transport,
            now=now,
        )
        assert result.action == "integration_observation_scheduled"
        assert result.replayed is False
        assert result.work.status == "succeeded"
        assert result.successor.work_kind == "candidate_observe_integrations"
        assert result.successor.work_key == authorization.deployment_id
        assert result.successor.status == "pending"
        assert calls == 1
    finally:
        store.__exit__(None, None, None)


def test_persisted_health_replay_requires_no_credential_or_network(tmp_path, monkeypatch) -> None:
    chain, authorization = _completed_window(tmp_path, monkeypatch)
    store = chain[0]
    now = START + timedelta(seconds=301)
    observe_supervisor_health_once(
        store,
        authorization.deployment_id,
        token=TOKEN,
        transport=lambda *_args: SUPERVISOR_HEALTHY,
        observed_at=now,
    )
    store.enqueue_work("candidate_observe_supervisor", authorization.deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_supervisor", now=now)
    assert item is not None
    try:
        result = execute_candidate_supervisor_observation_once(
            store,
            item,
            transport=lambda *_args: pytest.fail("replay performed a request"),
            now=now + timedelta(seconds=1),
        )
        assert result.replayed is True
        assert result.successor.status == "pending"
    finally:
        store.__exit__(None, None, None)


def test_unavailable_supervisor_is_transient_and_sanitized(tmp_path, monkeypatch) -> None:
    chain, _authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CandidateSupervisorObservationExecutionError) as caught:
            execute_candidate_supervisor_observation_once(
                store,
                item,
                token=TOKEN,
                transport=lambda *_args: (_ for _ in ()).throw(
                    TimeoutError("private transport detail")
                ),
                now=now,
            )
        assert caught.value.transient is True
        assert "private" not in str(caught.value).lower()
        assert store._connection.execute(
            "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe_integrations'"
        ).fetchone()[0] == 0
    finally:
        store.__exit__(None, None, None)


def test_tampered_core_window_is_deterministic_and_network_free(tmp_path, monkeypatch) -> None:
    chain, _authorization, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    store._connection.execute(
        "UPDATE core_health_window SET initial_health_sha256 = ?",
        ("f" * 64,),
    )
    store._connection.commit()
    try:
        with pytest.raises(CandidateSupervisorObservationExecutionError) as caught:
            execute_candidate_supervisor_observation_once(
                store,
                item,
                token=TOKEN,
                transport=lambda *_args: pytest.fail("tampered authority reached network"),
                now=now,
            )
        assert caught.value.transient is False
    finally:
        store.__exit__(None, None, None)
