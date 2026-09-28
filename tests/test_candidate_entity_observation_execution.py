from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp import candidate_entity_observation_execution as execution
from ha_syncapp.candidate_entity_observation_execution import (
    CandidateEntityObservationExecutionError,
    execute_candidate_entity_observation_once,
)
from ha_syncapp.entity_state_observation import observe_entity_states_once
from test_core_health_window import START, TOKEN
from test_entity_state_observation import _available
from test_integration_observation import FakeSession, _factory
from test_resource_availability_observation import _responses


def _running(tmp_path, monkeypatch, entity_ids=("light.kitchen",)):
    chain, authorization, target = _available(tmp_path, monkeypatch, entity_ids)
    store = chain[0]
    monkeypatch.setattr(execution, "_load_target", lambda *_args: target)
    now = START + timedelta(seconds=305)
    store.enqueue_work("candidate_observe_entities", authorization.deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_entities", now=now)
    assert item is not None
    return chain, authorization, target, item, now


def test_valid_states_atomically_schedule_automation_script_observation(
    tmp_path, monkeypatch
) -> None:
    chain, authorization, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = execute_candidate_entity_observation_once(
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
        assert result.action == "automation_script_observation_scheduled"
        assert result.outcome == "valid"
        assert result.replayed is False
        assert result.valid_count == result.expected_count == 1
        assert result.invalid_count == 0
        assert result.work.status == "succeeded"
        assert result.successor.work_kind == "candidate_observe_automation_scripts"
        assert result.successor.work_key == authorization.deployment_id
        assert result.successor.status == "pending"
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_invalid_states_atomically_schedule_finalization_only(tmp_path, monkeypatch, state) -> None:
    chain, _authorization, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = execute_candidate_entity_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(
                FakeSession(
                    _responses([{"entity_id": "light.kitchen", "state": state, "attributes": {}}])
                )
            ),
            now=now,
        )
        assert result.action == "finalization_scheduled"
        assert result.outcome == "invalid_states"
        assert result.invalid_count == 1
        assert result.successor.work_kind == "candidate_finalize"
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe_automation_scripts'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)


def test_invalid_state_replay_requires_no_credential_or_network(tmp_path, monkeypatch) -> None:
    chain, _authorization, target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    observe_entity_states_once(
        store,
        target,
        token=TOKEN,
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
        observed_at=now,
    )
    try:
        result = execute_candidate_entity_observation_once(
            store,
            item,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
            now=now + timedelta(seconds=1),
        )
        assert result.replayed is True
        assert result.action == "finalization_scheduled"
    finally:
        store.__exit__(None, None, None)


def test_transport_is_retryable_but_invalid_credential_is_deterministic(
    tmp_path, monkeypatch
) -> None:
    chain, _authorization, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CandidateEntityObservationExecutionError) as transient:
            execute_candidate_entity_observation_once(
                store,
                item,
                token=TOKEN,
                session_factory=lambda *_args: (_ for _ in ()).throw(
                    TimeoutError("private transport detail")
                ),
                now=now,
            )
        assert transient.value.transient is True
        assert "private" not in str(transient.value).lower()

        with pytest.raises(CandidateEntityObservationExecutionError) as deterministic:
            execute_candidate_entity_observation_once(
                store,
                item,
                token=" invalid ",
                session_factory=lambda *_args: pytest.fail("invalid credential opened a session"),
                now=now,
            )
        assert deterministic.value.transient is False
    finally:
        store.__exit__(None, None, None)


def test_malformed_snapshot_cannot_authorize_finalization(tmp_path, monkeypatch) -> None:
    chain, _authorization, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    duplicate = [
        {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
    ]
    try:
        with pytest.raises(CandidateEntityObservationExecutionError) as caught:
            execute_candidate_entity_observation_once(
                store,
                item,
                token=TOKEN,
                session_factory=_factory(FakeSession(_responses(duplicate))),
                now=now,
            )
        assert caught.value.transient is False
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='candidate_finalize'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)


def test_cross_branch_successor_conflict_rolls_back_completion(tmp_path, monkeypatch) -> None:
    chain, _authorization, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    original = execution.observe_entity_states_once

    def conflict(*args, **kwargs):
        observed = original(*args, **kwargs)
        store.enqueue_work("candidate_finalize", item.work_key, now=now)
        return observed

    monkeypatch.setattr(execution, "observe_entity_states_once", conflict)
    try:
        with pytest.raises(CandidateEntityObservationExecutionError) as caught:
            execute_candidate_entity_observation_once(
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
        assert caught.value.transient is False
        assert store._get_work(item.work_kind, item.work_key).status == "running"
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe_automation_scripts'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)
