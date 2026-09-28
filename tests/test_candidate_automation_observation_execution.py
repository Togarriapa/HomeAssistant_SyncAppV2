from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp import candidate_automation_observation_execution as execution
from ha_syncapp.automation_script_observation import observe_automation_scripts_once
from ha_syncapp.candidate_automation_observation_execution import (
    CandidateAutomationObservationExecutionError,
    execute_candidate_automation_observation_once,
)
from test_automation_script_observation import _valid
from test_core_health_window import START, TOKEN
from test_integration_observation import FakeSession, _factory
from test_resource_availability_observation import _responses


def _running(tmp_path, monkeypatch, entity_ids=("automation.arrival",)):
    chain, target = _valid(tmp_path, monkeypatch, entity_ids)
    store = chain[0]
    monkeypatch.setattr(execution, "_load_target", lambda *_args: target)
    now = START + timedelta(seconds=306)
    deployment_id = target.resource_target.deployment_id
    store.enqueue_work("candidate_observe_automation_scripts", deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_automation_scripts", now=now)
    assert item is not None
    return chain, target, item, now


def test_loaded_automations_atomically_schedule_assertions(tmp_path, monkeypatch) -> None:
    chain, target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = execute_candidate_automation_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(
                FakeSession(
                    _responses(
                        [
                            {
                                "entity_id": "automation.arrival",
                                "state": "on",
                                "attributes": {},
                            }
                        ]
                    )
                )
            ),
            now=now,
        )
        assert result.action == "assertion_observation_scheduled"
        assert result.outcome == "loaded"
        assert result.loaded_count == result.expected_count == 1
        assert result.failed_count == 0
        assert result.work.status == "succeeded"
        assert result.successor.work_kind == "candidate_observe_assertions"
        assert result.successor.work_key == target.resource_target.deployment_id
    finally:
        store.__exit__(None, None, None)


def test_failed_loading_atomically_schedules_finalization_only(tmp_path, monkeypatch) -> None:
    chain, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = execute_candidate_automation_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(FakeSession(_responses([]))),
            now=now,
        )
        assert result.action == "finalization_scheduled"
        assert result.outcome == "load_failed"
        assert result.failed_count == 1
        assert result.successor.work_kind == "candidate_finalize"
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe_assertions'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)


def test_load_failure_replay_is_credential_and_network_free(tmp_path, monkeypatch) -> None:
    chain, target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    observe_automation_scripts_once(
        store,
        target,
        token=TOKEN,
        observed_at=now,
        session_factory=_factory(FakeSession(_responses([]))),
    )
    try:
        result = execute_candidate_automation_observation_once(
            store,
            item,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
            now=now + timedelta(seconds=1),
        )
        assert result.replayed is True
        assert result.action == "finalization_scheduled"
    finally:
        store.__exit__(None, None, None)


def test_transport_retries_but_invalid_credential_blocks(tmp_path, monkeypatch) -> None:
    chain, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CandidateAutomationObservationExecutionError) as transient:
            execute_candidate_automation_observation_once(
                store,
                item,
                token=TOKEN,
                session_factory=lambda *_args: (_ for _ in ()).throw(
                    TimeoutError("private transport detail")
                ),
                now=now,
            )
        assert transient.value.transient is True

        with pytest.raises(CandidateAutomationObservationExecutionError) as deterministic:
            execute_candidate_automation_observation_once(
                store,
                item,
                token=" invalid ",
                session_factory=lambda *_args: pytest.fail(
                    "invalid credential opened a session"
                ),
                now=now,
            )
        assert deterministic.value.transient is False
    finally:
        store.__exit__(None, None, None)


def test_cross_branch_successor_conflict_rolls_back_completion(
    tmp_path, monkeypatch
) -> None:
    chain, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    original = execution.observe_automation_scripts_once

    def conflict(*args, **kwargs):
        observed = original(*args, **kwargs)
        store.enqueue_work("candidate_finalize", item.work_key, now=now)
        return observed

    monkeypatch.setattr(execution, "observe_automation_scripts_once", conflict)
    try:
        with pytest.raises(CandidateAutomationObservationExecutionError):
            execute_candidate_automation_observation_once(
                store,
                item,
                token=TOKEN,
                session_factory=_factory(
                    FakeSession(
                        _responses(
                            [
                                {
                                    "entity_id": "automation.arrival",
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
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe_assertions'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)
