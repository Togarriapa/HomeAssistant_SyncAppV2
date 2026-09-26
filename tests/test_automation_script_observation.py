from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from ha_syncapp.automation_script_observation import (
    AutomationScriptObservationError,
    derive_automation_script_target,
    load_automation_script_observation,
    observe_automation_scripts_once,
)
from ha_syncapp.entity_state_observation import observe_entity_states_once
from test_core_health_window import START, TOKEN
from test_entity_state_observation import _available
from test_integration_observation import FakeSession, _factory
from test_resource_availability_observation import _responses


def _valid(tmp_path: Path, monkeypatch, entity_ids):
    chain, authorization, resource_target = _available(tmp_path, monkeypatch, entity_ids)
    store = chain[0]
    states = [{"entity_id": entity, "state": "off", "attributes": {}} for entity in entity_ids]
    observe_entity_states_once(
        store,
        resource_target,
        token=TOKEN if entity_ids else None,
        observed_at=START + timedelta(seconds=305),
        session_factory=_factory(FakeSession(_responses(states))),
    )
    return chain, derive_automation_script_target(resource_target)


def test_derives_only_relevant_domains_and_persists_content_free_success(tmp_path, monkeypatch):
    chain, target = _valid(
        tmp_path,
        monkeypatch,
        ("automation.arrival", "light.kitchen", "script.notify_family"),
    )
    assert target.entity_ids == ("automation.arrival", "script.notify_family")
    store = chain[0]
    session = FakeSession(
        _responses(
            [
                {"entity_id": "automation.arrival", "state": "on", "attributes": {}},
                {"entity_id": "script.notify_family", "state": "off", "attributes": {}},
                {"entity_id": "light.kitchen", "state": "unavailable", "attributes": {}},
            ]
        )
    )
    try:
        result = observe_automation_scripts_once(
            store,
            target,
            token=TOKEN,
            observed_at=START + timedelta(seconds=306),
            session_factory=_factory(session),
        )
        assert result.status == "loaded"
        assert result.expected_count == result.loaded_count == 2
        assert result.failed_count == 0
        row = store._connection.execute("SELECT * FROM automation_script_observation").fetchone()
        assert row is not None
        assert "automation.arrival" not in repr(row)
        assert "script.notify_family" not in repr(row)
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("state", ["unknown", "unavailable", "idle"])
def test_non_loaded_state_is_durable_and_not_retried(tmp_path, monkeypatch, state):
    chain, target = _valid(tmp_path, monkeypatch, ("automation.arrival",))
    store = chain[0]
    try:
        first = observe_automation_scripts_once(
            store,
            target,
            token=TOKEN,
            observed_at=START + timedelta(seconds=306),
            session_factory=_factory(
                FakeSession(
                    _responses(
                        [{"entity_id": "automation.arrival", "state": state, "attributes": {}}]
                    )
                )
            ),
        )
        replay = observe_automation_scripts_once(
            store,
            target,
            session_factory=lambda *_args: pytest.fail("deterministic failure retried"),
        )
        assert first.status == replay.status == "load_failed"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_empty_relevant_set_and_success_replay_are_network_free(tmp_path, monkeypatch):
    chain, target = _valid(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    assert target.entity_ids == ()
    try:
        first = observe_automation_scripts_once(
            store,
            target,
            observed_at=START + timedelta(seconds=306),
            session_factory=lambda *_args: pytest.fail("empty target opened session"),
        )
        replay = observe_automation_scripts_once(
            store,
            target,
            session_factory=lambda *_args: pytest.fail("success replay opened session"),
        )
        assert first.status == "loaded"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_successful_entity_state_proof_is_required(tmp_path, monkeypatch):
    chain, authorization, resource_target = _available(
        tmp_path, monkeypatch, ("automation.arrival",)
    )
    store = chain[0]
    target = derive_automation_script_target(resource_target)
    try:
        with pytest.raises(AutomationScriptObservationError, match="state proof"):
            observe_automation_scripts_once(
                store,
                target,
                session_factory=lambda *_args: pytest.fail("unauthorized probe"),
            )
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "states",
    [
        [],
        [
            {"entity_id": "script.notify", "state": "off", "attributes": {}},
            {"entity_id": "script.notify", "state": "on", "attributes": {}},
        ],
        [{"entity_id": "script.notify", "state": None, "attributes": {}}],
    ],
    ids=("missing", "duplicate", "malformed"),
)
def test_invalid_relevant_entity_is_durable_and_not_retried(tmp_path, monkeypatch, states):
    chain, target = _valid(tmp_path, monkeypatch, ("script.notify",))
    store = chain[0]
    try:
        first = observe_automation_scripts_once(
            store,
            target,
            token=TOKEN,
            observed_at=START + timedelta(seconds=306),
            session_factory=_factory(FakeSession(_responses(states))),
        )
        replay = observe_automation_scripts_once(
            store,
            target,
            session_factory=lambda *_args: pytest.fail("deterministic failure retried"),
        )
        assert first.status == replay.status == "load_failed"
        assert first.failed_count == replay.failed_count == 1
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_persistence_failure_is_sanitized_and_retryable(tmp_path, monkeypatch):
    chain, target = _valid(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    store._connection.execute(
        "CREATE TRIGGER reject_automation_script BEFORE INSERT ON "
        "automation_script_observation "
        "BEGIN SELECT RAISE(ABORT, 'secret-storage-detail'); END"
    )
    try:
        with pytest.raises(AutomationScriptObservationError, match="state is invalid") as error:
            observe_automation_scripts_once(
                store,
                target,
                observed_at=START + timedelta(seconds=306),
            )
        assert "secret-storage-detail" not in str(error.value)
        assert load_automation_script_observation(store, target) is None
    finally:
        store.__exit__(None, None, None)


def test_tampering_and_schema_19_migration_fail_safe(tmp_path, monkeypatch):
    chain, target = _valid(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    root = store._root
    try:
        observe_automation_scripts_once(store, target, observed_at=START + timedelta(seconds=306))
        store._connection.execute(
            "UPDATE automation_script_observation SET target_sha256 = ?", ("f" * 64,)
        )
        store._connection.commit()
        with pytest.raises(AutomationScriptObservationError, match="state is invalid"):
            load_automation_script_observation(store, target)
        store._connection.execute("DROP TABLE automation_script_observation")
        store._connection.execute("PRAGMA user_version = 19")
        store._connection.commit()
    finally:
        store.__exit__(None, None, None)

    from ha_syncapp.state import StateStore

    with StateStore(root) as reopened:
        assert reopened._connection.execute("PRAGMA user_version").fetchone()[0] == 26
        assert reopened._connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'automation_script_observation'"
        ).fetchone() == ("automation_script_observation",)
