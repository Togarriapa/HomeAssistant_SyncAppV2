from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from ha_syncapp.entity_state_observation import (
    EntityStateObservationError,
    load_entity_state_observation,
    observe_entity_states_once,
)
from ha_syncapp.resource_availability_observation import (
    ResourceAvailabilityTarget,
    observe_changed_resources_once,
)
from test_core_health_window import START, TOKEN
from test_integration_observation import FakeSession, _factory
from test_resource_availability_observation import _ready, _responses


def _available(tmp_path: Path, monkeypatch, entity_ids=("light.kitchen",)):
    chain, authorization, prepared = _ready(tmp_path, monkeypatch)
    store = chain[0]
    target = ResourceAvailabilityTarget.create(prepared, entity_ids)
    states = [{"entity_id": entity_id, "state": "on", "attributes": {}} for entity_id in entity_ids]
    observe_changed_resources_once(
        store,
        target,
        token=TOKEN if entity_ids else None,
        observed_at=START + timedelta(seconds=304),
        session_factory=_factory(FakeSession(_responses(states))),
    )
    return chain, authorization, target


def test_valid_expected_states_persist_content_free_proof(tmp_path, monkeypatch):
    chain, authorization, target = _available(
        tmp_path, monkeypatch, ("light.kitchen", "sensor.outside")
    )
    store = chain[0]
    session = FakeSession(
        _responses(
            [
                {"entity_id": "sensor.outside", "state": "23.4", "attributes": {}},
                {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
                {"entity_id": "other.entity", "state": "unknown", "attributes": {}},
            ]
        )
    )
    try:
        result = observe_entity_states_once(
            store,
            target,
            token=TOKEN,
            observed_at=START + timedelta(seconds=305),
            session_factory=_factory(session),
        )
        assert result.status == "valid"
        assert result.valid_count == result.expected_count == 2
        assert result.invalid_count == 0
        assert session.sent[-1] == {"id": 1, "type": "get_states"}
        row = store._connection.execute("SELECT * FROM entity_state_observation").fetchone()
        assert row is not None
        assert "light.kitchen" not in repr(row)
        assert "sensor.outside" not in repr(row)
        assert "23.4" not in repr(row)
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("invalid", ["unknown", "unavailable"])
def test_invalid_expected_state_is_durable_and_not_retried(tmp_path, monkeypatch, invalid):
    chain, authorization, target = _available(tmp_path, monkeypatch)
    store = chain[0]
    try:
        first = observe_entity_states_once(
            store,
            target,
            token=TOKEN,
            observed_at=START + timedelta(seconds=305),
            session_factory=_factory(
                FakeSession(
                    _responses([{"entity_id": "light.kitchen", "state": invalid, "attributes": {}}])
                )
            ),
        )
        replay = observe_entity_states_once(
            store,
            target,
            session_factory=lambda *_args: pytest.fail("deterministic failure retried"),
        )
        assert first.status == replay.status == "invalid_states"
        assert first.invalid_count == replay.invalid_count == 1
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_empty_target_and_success_replay_are_credential_and_network_free(tmp_path, monkeypatch):
    chain, authorization, target = _available(tmp_path, monkeypatch, ())
    store = chain[0]
    try:
        first = observe_entity_states_once(
            store,
            target,
            observed_at=START + timedelta(seconds=305),
            session_factory=lambda *_args: pytest.fail("empty target opened a session"),
        )
        replay = observe_entity_states_once(
            store,
            target,
            session_factory=lambda *_args: pytest.fail("success replay opened a session"),
        )
        assert first.status == "valid"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_missing_or_malformed_expected_state_is_transport_failure_not_durable(
    tmp_path, monkeypatch
):
    chain, authorization, target = _available(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(EntityStateObservationError, match="unavailable"):
            observe_entity_states_once(
                store,
                target,
                token=TOKEN,
                observed_at=START + timedelta(seconds=305),
                session_factory=_factory(FakeSession(_responses([]))),
            )
        assert load_entity_state_observation(store, target) is None
    finally:
        store.__exit__(None, None, None)


def test_resource_availability_proof_is_required(tmp_path, monkeypatch):
    chain, authorization, prepared = _ready(tmp_path, monkeypatch)
    store = chain[0]
    target = ResourceAvailabilityTarget.create(prepared, ())
    try:
        with pytest.raises(EntityStateObservationError, match="availability proof"):
            observe_entity_states_once(
                store,
                target,
                session_factory=lambda *_args: pytest.fail("unauthorized probe"),
            )
    finally:
        store.__exit__(None, None, None)


def test_rebinding_tampering_and_temporal_drift_fail_closed(tmp_path, monkeypatch):
    chain, authorization, target = _available(tmp_path, monkeypatch, ())
    store = chain[0]
    try:
        observe_entity_states_once(store, target, observed_at=START + timedelta(seconds=305))
        store._connection.execute(
            "UPDATE entity_state_observation SET target_sha256 = ?", ("f" * 64,)
        )
        store._connection.commit()
        with pytest.raises(EntityStateObservationError, match="state is invalid"):
            load_entity_state_observation(store, target)
    finally:
        store.__exit__(None, None, None)


def test_observation_cannot_predate_availability_proof(tmp_path, monkeypatch):
    chain, authorization, target = _available(tmp_path, monkeypatch, ())
    store = chain[0]
    try:
        with pytest.raises(EntityStateObservationError, match="state is invalid"):
            observe_entity_states_once(store, target, observed_at=START + timedelta(seconds=303))
        assert load_entity_state_observation(store, target) is None
    finally:
        store.__exit__(None, None, None)


def test_persistence_failure_is_sanitized_and_retryable(tmp_path, monkeypatch):
    chain, authorization, target = _available(tmp_path, monkeypatch, ())
    store = chain[0]
    store._connection.execute(
        "CREATE TRIGGER reject_entity_state BEFORE INSERT ON entity_state_observation "
        "BEGIN SELECT RAISE(ABORT, 'secret-storage-detail'); END"
    )
    try:
        with pytest.raises(EntityStateObservationError, match="state is invalid") as error:
            observe_entity_states_once(store, target, observed_at=START + timedelta(seconds=305))
        assert "secret-storage-detail" not in str(error.value)
        assert load_entity_state_observation(store, target) is None
    finally:
        store.__exit__(None, None, None)


def test_schema_18_migrates_to_19(tmp_path, monkeypatch):
    chain, authorization, target = _available(tmp_path, monkeypatch, ())
    store = chain[0]
    root = store._root
    store._connection.execute("DROP TABLE IF EXISTS entity_state_observation")
    store._connection.execute("PRAGMA user_version = 18")
    store._connection.commit()
    store.__exit__(None, None, None)
    from ha_syncapp.state import StateStore

    with StateStore(root) as reopened:
        assert reopened._connection.execute("PRAGMA user_version").fetchone()[0] == 21
        assert reopened._connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'entity_state_observation'"
        ).fetchone() == ("entity_state_observation",)
