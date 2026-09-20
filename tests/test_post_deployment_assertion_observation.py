from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from ha_syncapp.automation_script_observation import observe_automation_scripts_once
from ha_syncapp.post_deployment_assertion_observation import (
    MAX_ASSERTIONS,
    PostDeploymentAssertion,
    PostDeploymentAssertionObservationError,
    derive_post_deployment_assertion_plan,
    evaluate_post_deployment_assertions_once,
    load_post_deployment_assertion_observation,
)
from test_automation_script_observation import _valid
from test_core_health_window import START, TOKEN
from test_integration_observation import FakeSession, _factory
from test_resource_availability_observation import _responses


def _ready(tmp_path: Path, monkeypatch, entity_ids):
    chain, automation_target = _valid(tmp_path, monkeypatch, entity_ids)
    store = chain[0]
    if automation_target.entity_ids:
        states = [
            {"entity_id": entity, "state": "off", "attributes": {}}
            for entity in automation_target.entity_ids
        ]
        observe_automation_scripts_once(
            store,
            automation_target,
            token=TOKEN,
            observed_at=START + timedelta(seconds=306),
            session_factory=_factory(FakeSession(_responses(states))),
        )
    else:
        observe_automation_scripts_once(
            store,
            automation_target,
            observed_at=START + timedelta(seconds=306),
        )
    return chain, derive_post_deployment_assertion_plan(automation_target)


def test_plan_is_versioned_canonical_and_derived_only_from_exact_target(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen", "sensor.temperature"))
    try:
        assert plan.schema_version == 1
        assert tuple(item.kind for item in plan.assertions) == (
            "entity_available",
            "entity_available",
        )
        assert tuple(item.entity_id for item in plan.assertions) == (
            "light.kitchen",
            "sensor.temperature",
        )
        assert plan.canonical_json == (
            '{"assertions":[{"entity_id":"light.kitchen","kind":"entity_available"},'
            '{"entity_id":"sensor.temperature","kind":"entity_available"}],"version":1}'
        )
    finally:
        chain[0].__exit__(None, None, None)


def test_success_requires_completed_exact_automation_script_proof(tmp_path, monkeypatch):
    chain, automation_target = _valid(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    plan = derive_post_deployment_assertion_plan(automation_target)
    try:
        with pytest.raises(PostDeploymentAssertionObservationError, match="load proof"):
            evaluate_post_deployment_assertions_once(
                store,
                plan,
                session_factory=lambda *_args: pytest.fail("unauthorized request"),
            )
    finally:
        store.__exit__(None, None, None)


def test_empty_plan_persists_explicit_success_without_credentials_or_network(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ())
    store = chain[0]
    try:
        first = evaluate_post_deployment_assertions_once(
            store,
            plan,
            observed_at=START + timedelta(seconds=307),
            session_factory=lambda *_args: pytest.fail("empty plan opened a session"),
        )
        replay = evaluate_post_deployment_assertions_once(
            store,
            plan,
            session_factory=lambda *_args: pytest.fail("replay opened a session"),
        )
        assert first.status == replay.status == "passed"
        assert first.evaluated_count == first.passed_count == first.failed_count == 0
        assert first.declared_count == first.skipped_count == 0
        assert first.derived_count == 0
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_pass_is_content_free_and_replays_without_credentials(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    try:
        first = evaluate_post_deployment_assertions_once(
            store,
            plan,
            token=TOKEN,
            observed_at=START + timedelta(seconds=307),
            session_factory=_factory(
                FakeSession(
                    _responses([{"entity_id": "light.kitchen", "state": "on", "attributes": {}}])
                )
            ),
        )
        replay = evaluate_post_deployment_assertions_once(
            store,
            plan,
            session_factory=lambda *_args: pytest.fail("success replay opened a session"),
        )
        assert first.status == replay.status == "passed"
        assert first.evaluated_count == first.passed_count == 1
        row = store._connection.execute(
            "SELECT * FROM post_deployment_assertion_observation"
        ).fetchone()
        assert row is not None
        assert "light.kitchen" not in repr(row)
        assert "on" not in repr(row)
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
        [{"entity_id": "light.kitchen", "state": None, "attributes": {}}],
        [{"entity_id": "light.kitchen", "state": "unavailable", "attributes": {}}],
        [{"entity_id": "light.kitchen", "state": "unknown", "attributes": {}}],
    ],
    ids=("missing", "duplicate", "malformed", "unavailable", "unknown"),
)
def test_false_assertion_is_durable_and_not_retried(tmp_path, monkeypatch, states):
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    try:
        first = evaluate_post_deployment_assertions_once(
            store,
            plan,
            token=TOKEN,
            observed_at=START + timedelta(seconds=307),
            session_factory=_factory(FakeSession(_responses(states))),
        )
        replay = evaluate_post_deployment_assertions_once(
            store,
            plan,
            session_factory=lambda *_args: pytest.fail("deterministic failure retried"),
        )
        assert first.status == replay.status == "failed"
        assert first.failed_count == 1
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_transport_failure_is_incomplete_retryable_and_sanitized(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    try:
        with pytest.raises(PostDeploymentAssertionObservationError, match="unavailable") as error:
            evaluate_post_deployment_assertions_once(
                store,
                plan,
                token=TOKEN,
                session_factory=_factory(FakeSession([RuntimeError("secret-transport")])),
            )
        assert "secret-transport" not in str(error.value)
        assert load_post_deployment_assertion_observation(store, plan) is None
    finally:
        store.__exit__(None, None, None)


def test_plan_tampering_and_bounds_fail_before_network(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    try:
        with pytest.raises(TypeError):
            PostDeploymentAssertion(kind="entity_available", entity_id="switch.intruder")
        original = plan.assertions
        object.__setattr__(plan, "assertions", original + original)
        with pytest.raises(PostDeploymentAssertionObservationError, match="state is invalid"):
            evaluate_post_deployment_assertions_once(
                store,
                plan,
                session_factory=lambda *_args: pytest.fail("duplicate plan opened session"),
            )
        object.__setattr__(plan, "assertions", original)
        object.__setattr__(plan, "schema_version", 2)
        with pytest.raises(PostDeploymentAssertionObservationError, match="state is invalid"):
            evaluate_post_deployment_assertions_once(
                store,
                plan,
                session_factory=lambda *_args: pytest.fail("invalid plan opened session"),
            )
        assert MAX_ASSERTIONS >= 1
    finally:
        store.__exit__(None, None, None)


def test_oversized_protocol_data_is_incomplete_and_retryable(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    try:
        with pytest.raises(PostDeploymentAssertionObservationError, match="unavailable"):
            evaluate_post_deployment_assertions_once(
                store,
                plan,
                token=TOKEN,
                max_message_bytes=16,
                session_factory=_factory(FakeSession(_responses([]))),
            )
        assert load_post_deployment_assertion_observation(store, plan) is None
    finally:
        store.__exit__(None, None, None)


def test_persistence_failure_is_sanitized_and_retryable(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ())
    store = chain[0]
    store._connection.execute(
        "CREATE TRIGGER reject_post_assertion BEFORE INSERT ON "
        "post_deployment_assertion_observation "
        "BEGIN SELECT RAISE(ABORT, 'secret-storage-detail'); END"
    )
    try:
        with pytest.raises(
            PostDeploymentAssertionObservationError, match="state is invalid"
        ) as error:
            evaluate_post_deployment_assertions_once(
                store,
                plan,
                observed_at=START + timedelta(seconds=307),
            )
        assert "secret-storage-detail" not in str(error.value)
        assert load_post_deployment_assertion_observation(store, plan) is None
    finally:
        store.__exit__(None, None, None)


def test_tampering_rebinding_temporal_order_and_schema_20_migration(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ())
    store = chain[0]
    root = store._root
    try:
        with pytest.raises(PostDeploymentAssertionObservationError, match="state is invalid"):
            evaluate_post_deployment_assertions_once(
                store,
                plan,
                observed_at=START + timedelta(seconds=305),
            )
        assert load_post_deployment_assertion_observation(store, plan) is None
        evaluate_post_deployment_assertions_once(
            store,
            plan,
            observed_at=START + timedelta(seconds=307),
        )
        store._connection.execute(
            "UPDATE post_deployment_assertion_observation SET assertion_set_sha256 = ?",
            ("f" * 64,),
        )
        store._connection.commit()
        with pytest.raises(PostDeploymentAssertionObservationError, match="state is invalid"):
            load_post_deployment_assertion_observation(store, plan)
        store._connection.execute("DROP TABLE post_deployment_assertion_observation")
        store._connection.execute("PRAGMA user_version = 20")
        store._connection.commit()
    finally:
        store.__exit__(None, None, None)

    from ha_syncapp.state import StateStore

    with StateStore(root) as reopened:
        assert reopened._connection.execute("PRAGMA user_version").fetchone()[0] == 21
        assert reopened._connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'post_deployment_assertion_observation'"
        ).fetchone() == ("post_deployment_assertion_observation",)
