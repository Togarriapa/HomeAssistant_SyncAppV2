from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_core_observation_execution import (
    CandidateCoreObservationExecutionError,
    execute_candidate_core_observation_once,
)
from ha_syncapp.core_health_observation import CoreHealthResponse
from ha_syncapp.core_health_window import load_core_health_window
from test_core_health_observation import _acknowledged

TOKEN = "candidate-observation-secret"
NOW = datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC)
HEALTHY = CoreHealthResponse(200, "application/json", b'{"message":"API running."}')


def _running(tmp_path, monkeypatch):
    chain, authorization = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    store.enqueue_work("candidate_observe", authorization.deployment_id, now=NOW)
    item = store.claim_work_kind("candidate_observe", now=NOW)
    assert item is not None
    return chain, authorization, item


def test_two_point_window_advances_one_action_per_invocation_and_hands_off_atomically(
    tmp_path, monkeypatch
) -> None:
    chain, authorization, item = _running(tmp_path, monkeypatch)
    store = chain[0]
    calls = 0

    def healthy(*_args):
        nonlocal calls
        calls += 1
        return HEALTHY

    try:
        initial = execute_candidate_core_observation_once(
            store,
            item,
            observation_seconds=300,
            token=TOKEN,
            transport=healthy,
            now=NOW,
        )
        assert initial.action == "initial_health_recorded"
        assert initial.work.status == "pending"
        assert initial.successor is None
        assert load_core_health_window(store, authorization.deployment_id) is None
        assert calls == 1

        item = store.claim_work_kind("candidate_observe", now=NOW + timedelta(seconds=1))
        assert item is not None
        started = execute_candidate_core_observation_once(
            store,
            item,
            observation_seconds=300,
            transport=lambda *_args: pytest.fail("window start performed a request"),
            now=NOW + timedelta(seconds=1),
        )
        assert started.action == "window_started"
        assert started.work.status == "retry"
        assert started.work.next_attempt_at == NOW + timedelta(seconds=301)
        assert calls == 1

        assert (
            store.claim_work_kind("candidate_observe", now=NOW + timedelta(seconds=300))
            is None
        )
        item = store.claim_work_kind("candidate_observe", now=NOW + timedelta(seconds=301))
        assert item is not None
        completed = execute_candidate_core_observation_once(
            store,
            item,
            observation_seconds=300,
            token=TOKEN,
            transport=healthy,
            now=NOW + timedelta(seconds=301),
        )
        assert completed.action == "supervisor_observation_scheduled"
        assert completed.work.status == "succeeded"
        assert completed.successor is not None
        assert completed.successor.work_kind == "candidate_observe_supervisor"
        assert completed.successor.work_key == authorization.deployment_id
        assert completed.successor.status == "pending"
        assert calls == 2
    finally:
        store.__exit__(None, None, None)


def test_unavailable_initial_health_is_transient_and_sanitized(tmp_path, monkeypatch) -> None:
    chain, _authorization, item = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CandidateCoreObservationExecutionError) as caught:
            execute_candidate_core_observation_once(
                store,
                item,
                observation_seconds=300,
                token=TOKEN,
                transport=lambda *_args: (_ for _ in ()).throw(
                    TimeoutError("private transport detail")
                ),
                now=NOW,
            )
        assert caught.value.transient is True
        assert "private" not in str(caught.value).lower()
    finally:
        store.__exit__(None, None, None)


def test_tampered_health_window_fails_deterministically_without_successor(
    tmp_path, monkeypatch
) -> None:
    chain, authorization, item = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        execute_candidate_core_observation_once(
            store,
            item,
            observation_seconds=300,
            token=TOKEN,
            transport=lambda *_args: HEALTHY,
            now=NOW,
        )
        item = store.claim_work_kind("candidate_observe", now=NOW + timedelta(seconds=1))
        assert item is not None
        execute_candidate_core_observation_once(
            store,
            item,
            observation_seconds=300,
            now=NOW + timedelta(seconds=1),
        )
        store._connection.execute(
            "UPDATE core_health_window SET initial_health_sha256 = ?",
            ("f" * 64,),
        )
        store._connection.commit()
        item = store._get_work("candidate_observe", authorization.deployment_id)
        store._connection.execute(
            "UPDATE work SET status='running',attempts=1,next_attempt_at=NULL "
            "WHERE work_kind='candidate_observe' AND work_key=?",
            (authorization.deployment_id,),
        )
        store._connection.commit()
        item = store._get_work("candidate_observe", authorization.deployment_id)
        with pytest.raises(CandidateCoreObservationExecutionError) as caught:
            execute_candidate_core_observation_once(
                store,
                item,
                observation_seconds=300,
                now=NOW + timedelta(seconds=301),
            )
        assert caught.value.transient is False
        assert store._connection.execute(
            "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe_supervisor'"
        ).fetchone()[0] == 0
    finally:
        store.__exit__(None, None, None)
