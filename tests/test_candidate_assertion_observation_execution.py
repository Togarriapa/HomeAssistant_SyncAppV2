from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp import candidate_assertion_observation_execution as execution
from ha_syncapp.candidate_assertion_observation_execution import (
    CandidateAssertionObservationExecutionError,
    execute_candidate_assertion_observation_once,
)
from ha_syncapp.post_deployment_assertion_observation import (
    evaluate_post_deployment_assertions_once,
)
from test_core_health_window import START, TOKEN
from test_integration_observation import FakeSession, _factory
from test_post_deployment_assertion_observation import _ready
from test_resource_availability_observation import _responses


def _running(tmp_path, monkeypatch, entity_ids=("light.kitchen",)):
    chain, plan = _ready(tmp_path, monkeypatch, entity_ids)
    store = chain[0]
    monkeypatch.setattr(execution, "_load_plan", lambda *_args: plan)
    now = START + timedelta(seconds=307)
    deployment_id = plan.automation_target.resource_target.deployment_id
    store.enqueue_work("candidate_observe_assertions", deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_assertions", now=now)
    assert item is not None
    return chain, plan, item, now


def test_passed_assertions_atomically_schedule_finalization(tmp_path, monkeypatch) -> None:
    chain, plan, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = execute_candidate_assertion_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(
                FakeSession(
                    _responses([{"entity_id": "light.kitchen", "state": "on", "attributes": {}}])
                )
            ),
            now=now,
        )
        assert result.action == "finalization_scheduled"
        assert result.outcome == "passed"
        assert result.passed_count == result.evaluated_count == 1
        assert result.failed_count == 0
        assert result.work.status == "succeeded"
        assert result.successor.work_kind == "candidate_finalize"
        assert result.successor.work_key == plan.automation_target.resource_target.deployment_id
    finally:
        store.__exit__(None, None, None)


def test_failed_assertions_also_schedule_finalization_with_failure_evidence(
    tmp_path, monkeypatch
) -> None:
    chain, _plan, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = execute_candidate_assertion_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(FakeSession(_responses([]))),
            now=now,
        )
        assert result.action == "finalization_scheduled"
        assert result.outcome == "failed"
        assert result.failed_count == 1
        assert result.successor.work_kind == "candidate_finalize"
    finally:
        store.__exit__(None, None, None)


def test_completed_failure_replays_without_credentials_or_network(tmp_path, monkeypatch) -> None:
    chain, plan, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    evaluate_post_deployment_assertions_once(
        store,
        plan,
        token=TOKEN,
        observed_at=now,
        session_factory=_factory(FakeSession(_responses([]))),
    )
    try:
        result = execute_candidate_assertion_observation_once(
            store,
            item,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
            now=now + timedelta(seconds=1),
        )
        assert result.replayed is True
        assert result.outcome == "failed"
    finally:
        store.__exit__(None, None, None)


def test_transport_retries_but_invalid_credential_blocks(tmp_path, monkeypatch) -> None:
    chain, _plan, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CandidateAssertionObservationExecutionError) as transient:
            execute_candidate_assertion_observation_once(
                store,
                item,
                token=TOKEN,
                session_factory=lambda *_args: (_ for _ in ()).throw(
                    TimeoutError("private transport detail")
                ),
                now=now,
            )
        assert transient.value.transient is True

        with pytest.raises(CandidateAssertionObservationExecutionError) as deterministic:
            execute_candidate_assertion_observation_once(
                store,
                item,
                token=" invalid ",
                session_factory=lambda *_args: pytest.fail("invalid token opened a session"),
                now=now,
            )
        assert deterministic.value.transient is False
    finally:
        store.__exit__(None, None, None)


def test_successor_conflict_rolls_back_work_completion(tmp_path, monkeypatch) -> None:
    chain, _plan, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    original = execution.evaluate_post_deployment_assertions_once

    def conflict(*args, **kwargs):
        observed = original(*args, **kwargs)
        store.enqueue_work("candidate_finalize", item.work_key, now=now)
        return observed

    monkeypatch.setattr(execution, "evaluate_post_deployment_assertions_once", conflict)
    try:
        with pytest.raises(CandidateAssertionObservationExecutionError):
            execute_candidate_assertion_observation_once(
                store,
                item,
                token=TOKEN,
                session_factory=_factory(
                    FakeSession(
                        _responses(
                            [
                                {
                                    "entity_id": "light.kitchen",
                                    "state": "on",
                                    "attributes": {},
                                }
                            ]
                        )
                    )
                ),
                now=now,
            )
        assert store._get_work(item.work_kind, item.work_key).status == "running"
    finally:
        store.__exit__(None, None, None)
