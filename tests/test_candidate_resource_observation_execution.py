from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ha_syncapp import candidate_resource_observation_execution as execution
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.candidate_resource_observation_execution import (
    CandidateResourceObservationExecutionError,
    execute_candidate_resource_observation_once,
)
from ha_syncapp.resource_availability_observation import (
    ResourceAvailabilityError,
    ResourceAvailabilityTarget,
)
from ha_syncapp.state import StateStore
from semantic_fixtures import candidate_inputs
from test_core_health_window import START, TOKEN
from test_integration_observation import FakeSession, _factory
from test_resource_availability_observation import _ready, _responses


def _running(tmp_path, monkeypatch, entity_ids=("light.kitchen",)):
    chain, authorization, prepared = _ready(tmp_path, monkeypatch)
    store = chain[0]
    target = ResourceAvailabilityTarget.create(prepared, entity_ids)
    monkeypatch.setattr(execution, "_load_target", lambda *_args: target)
    now = START + timedelta(seconds=304)
    store.enqueue_work("candidate_observe_resources", authorization.deployment_id, now=now)
    item = store.claim_work_kind("candidate_observe_resources", now=now)
    assert item is not None
    return chain, authorization, target, item, now


def test_available_resources_atomically_schedule_entity_observation(tmp_path, monkeypatch) -> None:
    chain, authorization, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    session = FakeSession(
        _responses([{"entity_id": "light.kitchen", "state": "on", "attributes": {}}])
    )
    try:
        result = execute_candidate_resource_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(session),
            now=now,
        )
        assert result.action == "entity_observation_scheduled"
        assert result.outcome == "available"
        assert result.replayed is False
        assert result.expected_count == result.available_count == 1
        assert result.work.status == "succeeded"
        assert result.successor.work_kind == "candidate_observe_entities"
        assert result.successor.work_key == authorization.deployment_id
        assert result.successor.status == "pending"
    finally:
        store.__exit__(None, None, None)


def test_missing_resources_atomically_schedule_finalization_only(tmp_path, monkeypatch) -> None:
    chain, _authorization, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = execute_candidate_resource_observation_once(
            store,
            item,
            token=TOKEN,
            session_factory=_factory(FakeSession(_responses([]))),
            now=now,
        )
        assert result.action == "finalization_scheduled"
        assert result.outcome == "missing_resources"
        assert result.available_count == 0
        assert result.successor.work_kind == "candidate_finalize"
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe_entities'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)


def test_missing_resource_replay_requires_no_credential_or_network(tmp_path, monkeypatch) -> None:
    chain, _authorization, target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    from ha_syncapp.resource_availability_observation import observe_changed_resources_once

    observe_changed_resources_once(
        store,
        target,
        token=TOKEN,
        session_factory=_factory(FakeSession(_responses([]))),
        observed_at=now,
    )
    try:
        result = execute_candidate_resource_observation_once(
            store,
            item,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
            now=now + timedelta(seconds=1),
        )
        assert result.replayed is True
        assert result.action == "finalization_scheduled"
    finally:
        store.__exit__(None, None, None)


def test_transient_transport_and_deterministic_invalid_credential_are_sanitized(
    tmp_path, monkeypatch
) -> None:
    chain, _authorization, _target, item, now = _running(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(CandidateResourceObservationExecutionError) as transient:
            execute_candidate_resource_observation_once(
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

        with pytest.raises(CandidateResourceObservationExecutionError) as deterministic:
            execute_candidate_resource_observation_once(
                store,
                item,
                token=" invalid ",
                session_factory=lambda *_args: pytest.fail("invalid credential opened a session"),
                now=now,
            )
        assert deterministic.value.transient is False
    finally:
        store.__exit__(None, None, None)


def test_target_is_derived_from_exact_persisted_candidate_evidence(tmp_path, monkeypatch) -> None:
    inputs_root = tmp_path / "inputs"
    inputs_root.mkdir()
    _, _, _, dependencies, impact, risk, runtime, version = candidate_inputs(
        inputs_root, {"automations.yaml": b"alias: safe\n"}
    )
    evidence = CandidateBackupEvidence(
        risk.target,
        risk.repository_id,
        risk.baseline_sha,
        risk.candidate_sha,
        risk.stage_manifest_sha256,
        risk.runtime_sha256,
        risk.level,
        version.version,
        "backup_123",
    )
    state_root = tmp_path / "state"
    state_root.mkdir()
    with StateStore(state_root) as store:
        store.bind_repository(evidence.target, evidence.repository_id)
        prepared = store.record_prepared_deployment(str(uuid4()), evidence)
        monkeypatch.setattr(
            execution,
            "load_candidate_dependency_checkpoint",
            lambda *_args: SimpleNamespace(
                phase="completed",
                dependencies=lambda: dependencies,
                runtime=lambda: runtime,
            ),
        )
        monkeypatch.setattr(
            execution,
            "load_candidate_risk_checkpoint",
            lambda *_args: SimpleNamespace(
                phase="completed",
                impact=lambda *_args: impact,
                risk=lambda *_args: risk,
            ),
        )
        target = execution._load_target(store, prepared.deployment_id)
        assert target.entity_ids == risk.affected_entities

        monkeypatch.setattr(
            execution,
            "load_candidate_risk_checkpoint",
            lambda *_args: SimpleNamespace(
                phase="completed",
                impact=lambda *_args: impact,
                risk=lambda *_args: replace(risk, candidate_sha="e" * 40),
            ),
        )
        with pytest.raises(ResourceAvailabilityError) as caught:
            execution._load_target(store, prepared.deployment_id)
        assert caught.value.transient is False
