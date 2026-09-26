from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.prepared_deployment import PreparedDeployment
from ha_syncapp.resource_availability_observation import (
    ResourceAvailabilityError,
    ResourceAvailabilityTarget,
    derive_resource_availability_target,
    load_resource_availability_observation,
    observe_changed_resources_once,
)
from ha_syncapp.startup_error_observation import observe_startup_errors_once
from semantic_fixtures import candidate_inputs
from test_core_health_window import START, TOKEN
from test_integration_observation import FakeSession, _factory
from test_startup_error_observation import _prepared, _startup_responses


def _ready(tmp_path: Path, monkeypatch):
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    observe_startup_errors_once(
        store,
        authorization.deployment_id,
        token=TOKEN,
        observed_at=START + timedelta(seconds=303),
        session_factory=_factory(FakeSession(_startup_responses([]))),
    )
    prepared = store.prepared_deployment(authorization.deployment_id)
    assert prepared is not None
    return chain, authorization, prepared


def _responses(states: list[object]) -> list[object]:
    return [
        {"type": "auth_required", "ha_version": "2026.9.3"},
        {"type": "auth_ok", "ha_version": "2026.9.3"},
        {"id": 1, "type": "result", "success": True, "result": states},
    ]


def test_records_one_bounded_get_states_and_content_free_proof(tmp_path, monkeypatch):
    chain, authorization, prepared = _ready(tmp_path, monkeypatch)
    store = chain[0]
    target = ResourceAvailabilityTarget.create(prepared, ("light.kitchen", "sensor.outside"))
    session = FakeSession(
        _responses(
            [
                {"entity_id": "sensor.outside", "state": "unknown", "attributes": {}},
                {"entity_id": "light.kitchen", "state": "unavailable", "attributes": {}},
                {"entity_id": "other.entity", "state": "on", "attributes": {}},
            ]
        )
    )
    try:
        result = observe_changed_resources_once(
            store,
            target,
            token=TOKEN,
            observed_at=START + timedelta(seconds=304),
            session_factory=_factory(session),
        )
        assert result.status == "available"
        assert result.expected_count == result.available_count == 2
        assert session.sent == [
            {"access_token": TOKEN, "type": "auth"},
            {"id": 1, "type": "get_states"},
        ]
        row = store._connection.execute(
            "SELECT * FROM resource_availability_observation"
        ).fetchone()
        assert row is not None
        assert "light.kitchen" not in repr(row)
        assert "sensor.outside" not in repr(row)
        assert "unavailable" not in repr(row)
    finally:
        store.__exit__(None, None, None)


def test_empty_target_and_completed_replay_need_no_credentials_or_network(tmp_path, monkeypatch):
    chain, authorization, prepared = _ready(tmp_path, monkeypatch)
    store = chain[0]
    target = ResourceAvailabilityTarget.create(prepared, ())
    try:
        first = observe_changed_resources_once(
            store,
            target,
            observed_at=START + timedelta(seconds=304),
            session_factory=lambda *_args: pytest.fail("empty target opened a session"),
        )
        replay = observe_changed_resources_once(
            store,
            target,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
        )
        assert first.replayed is False
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "states",
    [
        [],
        [
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
            {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
        ],
        [{"entity_id": True, "state": "on", "attributes": {}}],
        [{"entity_id": "light.kitchen", "state": "on"}],
    ],
)
def test_missing_duplicate_or_malformed_state_persists_no_success(tmp_path, monkeypatch, states):
    chain, authorization, prepared = _ready(tmp_path, monkeypatch)
    store = chain[0]
    target = ResourceAvailabilityTarget.create(prepared, ("light.kitchen",))
    try:
        with pytest.raises(ResourceAvailabilityError, match="unavailable"):
            observe_changed_resources_once(
                store,
                target,
                token=TOKEN,
                observed_at=START + timedelta(seconds=304),
                session_factory=_factory(FakeSession(_responses(states))),
            )
        assert load_resource_availability_observation(store, target) is None
    finally:
        store.__exit__(None, None, None)


def test_significant_startup_errors_block_resource_probe(tmp_path, monkeypatch):
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    prepared = store.prepared_deployment(authorization.deployment_id)
    assert prepared is not None
    from test_startup_error_observation import _entry

    observe_startup_errors_once(
        store,
        authorization.deployment_id,
        token=TOKEN,
        observed_at=START + timedelta(seconds=303),
        session_factory=_factory(
            FakeSession(
                _startup_responses(
                    [_entry(level="ERROR", timestamp=(START + timedelta(seconds=302)).timestamp())]
                )
            )
        ),
    )
    try:
        with pytest.raises(ResourceAvailabilityError, match="clear"):
            observe_changed_resources_once(
                store,
                ResourceAvailabilityTarget.create(prepared, ()),
                session_factory=lambda *_args: pytest.fail("blocked probe opened session"),
            )
    finally:
        store.__exit__(None, None, None)


def test_target_rebinding_and_persisted_tampering_fail_closed(tmp_path, monkeypatch):
    chain, authorization, prepared = _ready(tmp_path, monkeypatch)
    store = chain[0]
    target = ResourceAvailabilityTarget.create(prepared, ())
    try:
        observe_changed_resources_once(store, target, observed_at=START + timedelta(seconds=304))
        changed = ResourceAvailabilityTarget.create(prepared, ("light.kitchen",))
        with pytest.raises(ResourceAvailabilityError, match="state is invalid"):
            load_resource_availability_observation(store, changed)
        store._connection.execute("UPDATE resource_availability_observation SET expected_count = 1")
        store._connection.commit()
        with pytest.raises(ResourceAvailabilityError, match="state is invalid"):
            load_resource_availability_observation(store, target)
    finally:
        store.__exit__(None, None, None)


def test_schema_17_migrates_to_18(tmp_path, monkeypatch):
    chain, authorization, prepared = _ready(tmp_path, monkeypatch)
    store = chain[0]
    root = store._root
    store._connection.execute("DROP TABLE IF EXISTS resource_availability_observation")
    store._connection.execute("PRAGMA user_version = 17")
    store._connection.commit()
    store.__exit__(None, None, None)
    from ha_syncapp.state import StateStore

    with StateStore(root) as reopened:
        assert reopened._connection.execute("PRAGMA user_version").fetchone()[0] == 27
        assert reopened._connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'resource_availability_observation'"
        ).fetchone() == ("resource_availability_observation",)


def test_target_is_derived_only_from_reverified_candidate_evidence(tmp_path):
    inputs = candidate_inputs(tmp_path, {"automations.yaml": b"alias: safe\n"})
    _, _, _, dependencies, impact, risk, runtime, version = inputs
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
    prepared = PreparedDeployment("12345678-1234-5678-9234-567812345678", evidence, START)
    target = derive_resource_availability_target(prepared, dependencies, impact, risk, runtime)
    assert target.entity_ids == risk.affected_entities

    from dataclasses import replace

    with pytest.raises(ResourceAvailabilityError, match="state is invalid"):
        derive_resource_availability_target(
            prepared, dependencies, impact, replace(risk, candidate_sha="c" * 40), runtime
        )
