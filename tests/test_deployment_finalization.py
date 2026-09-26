from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from ha_syncapp.automation_script_observation import (
    derive_automation_script_target,
    observe_automation_scripts_once,
)
from ha_syncapp.deployment_finalization import (
    DeploymentFinalizationError,
    candidate_finalization_authority,
    finalize_deployment_once,
    is_candidate_blocked_by_finalization,
    load_deployment_finalization,
)
from ha_syncapp.entity_state_observation import observe_entity_states_once
from ha_syncapp.post_deployment_assertion_observation import (
    derive_post_deployment_assertion_plan,
    evaluate_post_deployment_assertions_once,
)
from ha_syncapp.resource_availability_observation import ResourceAvailabilityTarget
from ha_syncapp.startup_error_observation import observe_startup_errors_once
from test_automation_script_observation import _valid as _valid_automation
from test_core_health_window import START, TOKEN
from test_entity_state_observation import _available
from test_integration_observation import FakeSession, _factory
from test_post_deployment_assertion_observation import _ready
from test_resource_availability_observation import _responses
from test_startup_error_observation import (
    StartupSession,
    _entry,
    _prepared,
    _startup_responses,
)


def _successful(tmp_path: Path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ())
    evaluate_post_deployment_assertions_once(
        chain[0], plan, observed_at=START + timedelta(seconds=307)
    )
    return chain, plan


def test_full_success_is_immutable_promotion_authority_and_network_free(tmp_path, monkeypatch):
    chain, plan = _successful(tmp_path, monkeypatch)
    store = chain[0]
    try:
        first = finalize_deployment_once(
            store,
            plan,
            finalized_at=START + timedelta(seconds=308),
        )
        replay = finalize_deployment_once(store, plan)
        assert first.outcome == replay.outcome == "success"
        assert first.authority == replay.authority == "promote_and_tag"
        assert first.failure_stage == replay.failure_stage == "none"
        assert first.candidate_blocked is replay.candidate_blocked is False
        assert replay.replayed is True
        assert candidate_finalization_authority(store, plan) == "promote_and_tag"
    finally:
        store.__exit__(None, None, None)


def test_assertion_failure_is_durable_rollback_authority_and_blocks_candidate(
    tmp_path, monkeypatch
):
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
    try:
        first = finalize_deployment_once(
            store,
            plan,
            finalized_at=START + timedelta(seconds=308),
        )
        replay = finalize_deployment_once(store, plan)
        assert first.outcome == replay.outcome == "failure"
        assert first.authority == replay.authority == "rollback"
        assert first.failure_stage == "post_deployment_assertions"
        assert first.candidate_blocked is True
        assert replay.replayed is True
        assert candidate_finalization_authority(store, plan) == "rollback"
        candidate_sha = plan.automation_target.resource_target.candidate_sha
        assert is_candidate_blocked_by_finalization(store, plan, candidate_sha)
        assert not is_candidate_blocked_by_finalization(store, plan, "f" * 40)
    finally:
        store.__exit__(None, None, None)


def test_entity_failure_finalizes_without_fabricating_downstream_evidence(tmp_path, monkeypatch):
    chain, authorization, resource_target = _available(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    observe_entity_states_once(
        store,
        resource_target,
        token=TOKEN,
        observed_at=START + timedelta(seconds=305),
        session_factory=_factory(
            FakeSession(
                _responses(
                    [
                        {
                            "entity_id": "light.kitchen",
                            "state": "unknown",
                            "attributes": {},
                        }
                    ]
                )
            )
        ),
    )
    plan = derive_post_deployment_assertion_plan(derive_automation_script_target(resource_target))
    try:
        result = finalize_deployment_once(
            store,
            plan,
            finalized_at=START + timedelta(seconds=306),
        )
        assert result.outcome == "failure"
        assert result.failure_stage == "entity_states"
        assert result.authority == "rollback"
    finally:
        store.__exit__(None, None, None)


def test_startup_failure_is_the_terminal_predicate(tmp_path, monkeypatch):
    chain, authorization = _prepared(tmp_path, monkeypatch)
    store = chain[0]
    observe_startup_errors_once(
        store,
        authorization.deployment_id,
        token=TOKEN,
        observed_at=START + timedelta(seconds=303),
        session_factory=_factory(
            StartupSession(
                _startup_responses(
                    [
                        _entry(
                            level="ERROR",
                            timestamp=(START + timedelta(seconds=302)).timestamp(),
                        )
                    ]
                )
            )
        ),
    )
    prepared = store.prepared_deployment(authorization.deployment_id)
    assert prepared is not None
    target = ResourceAvailabilityTarget.create(prepared, ())
    plan = derive_post_deployment_assertion_plan(derive_automation_script_target(target))
    try:
        result = finalize_deployment_once(store, plan, finalized_at=START + timedelta(seconds=304))
        assert result.outcome == "failure"
        assert result.failure_stage == "startup_errors"
        assert result.completed_predicate_count == 1
    finally:
        store.__exit__(None, None, None)


def test_automation_failure_is_the_terminal_predicate(tmp_path, monkeypatch):
    chain, target = _valid_automation(tmp_path, monkeypatch, ("automation.arrival",))
    store = chain[0]
    observe_automation_scripts_once(
        store,
        target,
        token=TOKEN,
        observed_at=START + timedelta(seconds=306),
        session_factory=_factory(
            FakeSession(
                _responses(
                    [
                        {
                            "entity_id": "automation.arrival",
                            "state": "unavailable",
                            "attributes": {},
                        }
                    ]
                )
            )
        ),
    )
    plan = derive_post_deployment_assertion_plan(target)
    try:
        result = finalize_deployment_once(store, plan, finalized_at=START + timedelta(seconds=307))
        assert result.outcome == "failure"
        assert result.failure_stage == "automation_script_load"
        assert result.completed_predicate_count == 4
    finally:
        store.__exit__(None, None, None)


def test_incomplete_chain_never_becomes_success_or_failure(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ())
    store = chain[0]
    try:
        with pytest.raises(DeploymentFinalizationError, match="incomplete"):
            finalize_deployment_once(store, plan)
        assert load_deployment_finalization(store, plan) is None
        assert candidate_finalization_authority(store, plan) == "none"
    finally:
        store.__exit__(None, None, None)


def test_failure_rejects_fabricated_downstream_evidence(tmp_path, monkeypatch):
    chain, authorization, resource_target = _available(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    observe_entity_states_once(
        store,
        resource_target,
        token=TOKEN,
        observed_at=START + timedelta(seconds=305),
        session_factory=_factory(
            FakeSession(
                _responses([{"entity_id": "light.kitchen", "state": "unknown", "attributes": {}}])
            )
        ),
    )
    plan = derive_post_deployment_assertion_plan(derive_automation_script_target(resource_target))
    store._connection.execute(
        "INSERT INTO post_deployment_assertion_observation VALUES "
        "(?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, ?)",
        (
            resource_target.deployment_id,
            "a" * 64,
            resource_target.target_sha256,
            plan.assertion_set_sha256,
            (START + timedelta(seconds=306)).isoformat(),
            "b" * 64,
        ),
    )
    store._connection.commit()
    try:
        with pytest.raises(DeploymentFinalizationError, match="state is invalid"):
            finalize_deployment_once(store, plan)
    finally:
        store.__exit__(None, None, None)


def test_persistence_failure_is_sanitized_and_retryable(tmp_path, monkeypatch):
    chain, plan = _successful(tmp_path, monkeypatch)
    store = chain[0]
    store._connection.execute(
        "CREATE TRIGGER reject_finalization BEFORE INSERT ON deployment_finalization "
        "BEGIN SELECT RAISE(ABORT, 'secret-storage-detail'); END"
    )
    try:
        with pytest.raises(DeploymentFinalizationError, match="state is invalid") as error:
            finalize_deployment_once(
                store,
                plan,
                finalized_at=START + timedelta(seconds=308),
            )
        assert "secret-storage-detail" not in str(error.value)
        assert load_deployment_finalization(store, plan) is None
    finally:
        store.__exit__(None, None, None)


def test_tampering_rebinding_temporal_order_and_schema_21_migration(tmp_path, monkeypatch):
    chain, plan = _successful(tmp_path, monkeypatch)
    store = chain[0]
    root = store._root
    try:
        with pytest.raises(DeploymentFinalizationError, match="state is invalid"):
            finalize_deployment_once(
                store,
                plan,
                finalized_at=START + timedelta(seconds=306),
            )
        finalize_deployment_once(
            store,
            plan,
            finalized_at=START + timedelta(seconds=308),
        )
        store._connection.execute(
            "UPDATE deployment_finalization SET backup_slug = ?", ("rebound-backup",)
        )
        store._connection.commit()
        with pytest.raises(DeploymentFinalizationError, match="state is invalid"):
            load_deployment_finalization(store, plan)
        store._connection.execute("DROP TABLE deployment_finalization")
        store._connection.execute("PRAGMA user_version = 21")
        store._connection.commit()
    finally:
        store.__exit__(None, None, None)

    from ha_syncapp.state import StateStore

    with StateStore(root) as reopened:
        assert reopened._connection.execute("PRAGMA user_version").fetchone()[0] == 28
        assert reopened._connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'deployment_finalization'"
        ).fetchone() == ("deployment_finalization",)
