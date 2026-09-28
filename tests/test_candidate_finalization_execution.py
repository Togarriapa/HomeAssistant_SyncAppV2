from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp import candidate_finalization_execution as execution
from ha_syncapp.candidate_finalization_execution import (
    CandidateFinalizationExecutionError,
    execute_candidate_finalization_once,
)
from ha_syncapp.post_deployment_assertion_observation import (
    evaluate_post_deployment_assertions_once,
)
from test_core_health_window import START, TOKEN
from test_deployment_finalization import _successful
from test_integration_observation import FakeSession, _factory
from test_post_deployment_assertion_observation import _ready
from test_resource_availability_observation import _responses


def _running(tmp_path, monkeypatch, *, complete: bool = True):
    if complete:
        chain, plan = _successful(tmp_path, monkeypatch)
    else:
        chain, plan = _ready(tmp_path, monkeypatch, ())
    store = chain[0]
    monkeypatch.setattr(execution, "_load_plan", lambda *_args: plan)
    now = START + timedelta(seconds=308)
    deployment_id = plan.automation_target.resource_target.deployment_id
    store.enqueue_work("candidate_finalize", deployment_id, now=now)
    item = store.claim_work_kind("candidate_finalize", now=now)
    assert item is not None
    return store, plan, item, now


def test_success_atomically_schedules_promotion_handoff(tmp_path, monkeypatch) -> None:
    store, _plan, item, now = _running(tmp_path, monkeypatch)
    try:
        result = execute_candidate_finalization_once(store, item, now=now)
        assert result.action == "promotion_scheduled"
        assert result.outcome == "success"
        assert result.authority == "promote_and_tag"
        assert result.candidate_blocked is False
        assert result.work.status == "succeeded"
        assert result.successor.work_kind == "candidate_promote"
        assert result.successor.status == "pending"
    finally:
        store.__exit__(None, None, None)


def test_failure_atomically_schedules_only_rollback_handoff(tmp_path, monkeypatch) -> None:
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    evaluate_post_deployment_assertions_once(
        store,
        plan,
        token=TOKEN,
        observed_at=START + timedelta(seconds=307),
        session_factory=_factory(
            FakeSession(
                _responses(
                    [
                        {
                            "entity_id": "light.kitchen",
                            "state": "unavailable",
                            "attributes": {},
                        }
                    ]
                )
            )
        ),
    )
    monkeypatch.setattr(execution, "_load_plan", lambda *_args: plan)
    now = START + timedelta(seconds=308)
    deployment_id = plan.automation_target.resource_target.deployment_id
    store.enqueue_work("candidate_finalize", deployment_id, now=now)
    item = store.claim_work_kind("candidate_finalize", now=now)
    assert item is not None
    try:
        result = execute_candidate_finalization_once(store, item, now=now)
        assert result.action == "rollback_scheduled"
        assert result.authority == "rollback"
        assert result.candidate_blocked is True
        assert result.successor.work_kind == "candidate_rollback"
        assert store._connection.execute(
            "SELECT COUNT(*) FROM work WHERE work_kind='candidate_promote'"
        ).fetchone() == (0,)
    finally:
        store.__exit__(None, None, None)


def test_incomplete_chain_is_transient_and_leaves_work_running(tmp_path, monkeypatch) -> None:
    store, _plan, item, now = _running(tmp_path, monkeypatch, complete=False)
    try:
        with pytest.raises(CandidateFinalizationExecutionError) as error:
            execute_candidate_finalization_once(store, item, now=now)
        assert error.value.transient is True
        assert store._get_work(item.work_kind, item.work_key).status == "running"
    finally:
        store.__exit__(None, None, None)


def test_successor_conflict_rolls_back_work_completion(tmp_path, monkeypatch) -> None:
    store, _plan, item, now = _running(tmp_path, monkeypatch)
    store.enqueue_work("candidate_rollback", item.work_key, now=now)
    try:
        with pytest.raises(CandidateFinalizationExecutionError) as error:
            execute_candidate_finalization_once(store, item, now=now)
        assert error.value.transient is False
        assert store._get_work(item.work_kind, item.work_key).status == "running"
    finally:
        store.__exit__(None, None, None)
