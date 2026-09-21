from __future__ import annotations

from datetime import timedelta

import pytest

from ha_syncapp.deployment_rollback import (
    RollbackBackupProof,
    RollbackRepositoryProof,
    authorize_deployment_rollback_once,
)
from ha_syncapp.deployment_rollback_retrigger import (
    DeploymentRollbackRetriggerError,
    load_rollback_recovery_plan,
)
from ha_syncapp.post_deployment_assertion_observation import (
    evaluate_post_deployment_assertions_once,
)
from test_core_health_window import START, TOKEN
from test_deployment_finalization import _failed as _failed_finalization
from test_integration_observation import FakeSession, _factory
from test_resource_availability_observation import _responses


def _authorized(tmp_path, monkeypatch):
    chain, plan = _failed_finalization(tmp_path, monkeypatch, ("light.kitchen",))
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
    prepared = store.prepared_deployment(plan.automation_target.resource_target.deployment_id)
    assert prepared is not None
    result = authorize_deployment_rollback_once(
        store,
        plan,
        github_token=TOKEN,
        supervisor_token=TOKEN,
        repository_reader=lambda *_args: RollbackRepositoryProof(
            prepared.evidence.repository_id, True, prepared.evidence.baseline_sha
        ),
        backup_reader=lambda *_args: RollbackBackupProof(
            prepared.evidence.backup_slug,
            "full",
            prepared.evidence.core_version,
            True,
            True,
        ),
        observed_at=START + timedelta(seconds=309),
    )
    return store, plan, result.intent


def test_authorization_persists_exact_reconstructable_plan(tmp_path, monkeypatch):
    store, original_plan, intent = _authorized(tmp_path, monkeypatch)
    try:
        recovered = load_rollback_recovery_plan(store, intent)
        assert recovered.assertion_set_sha256 == original_plan.assertion_set_sha256
        assert recovered.canonical_json == original_plan.canonical_json
        assert (
            recovered.automation_target.target_sha256
            == original_plan.automation_target.target_sha256
        )
        assert (
            recovered.automation_target.resource_target.target_sha256
            == original_plan.automation_target.resource_target.target_sha256
        )
        assert recovered.automation_target.resource_target.entity_ids == ("light.kitchen",)
        assert recovered.automation_target.resource_target.candidate_sha == intent.candidate_sha
        assert recovered.automation_target.resource_target.deployment_id == intent.deployment_id
    finally:
        store.__exit__(None, None, None)


def test_recovery_authority_rejects_candidate_rebinding(tmp_path, monkeypatch):
    store, _plan, intent = _authorized(tmp_path, monkeypatch)
    try:
        store._connection.execute(
            "UPDATE rollback_recovery_authority SET candidate_sha = ? WHERE deployment_id = ?",
            ("f" * 40, intent.deployment_id),
        )
        store._connection.commit()
        with pytest.raises(DeploymentRollbackRetriggerError, match="authority"):
            load_rollback_recovery_plan(store, intent)
    finally:
        store.__exit__(None, None, None)


def test_recovery_authority_rejects_entity_tampering(tmp_path, monkeypatch):
    store, _plan, intent = _authorized(tmp_path, monkeypatch)
    try:
        store._connection.execute(
            "UPDATE rollback_recovery_authority SET entity_ids_json = ? WHERE deployment_id = ?",
            ('["switch.garage"]', intent.deployment_id),
        )
        store._connection.commit()
        with pytest.raises(DeploymentRollbackRetriggerError, match="authority"):
            load_rollback_recovery_plan(store, intent)
    finally:
        store.__exit__(None, None, None)


def test_missing_recovery_authority_fails_closed(tmp_path, monkeypatch):
    store, _plan, intent = _authorized(tmp_path, monkeypatch)
    try:
        store._connection.execute(
            "DELETE FROM rollback_recovery_authority WHERE deployment_id = ?",
            (intent.deployment_id,),
        )
        store._connection.commit()
        with pytest.raises(DeploymentRollbackRetriggerError, match="authority"):
            load_rollback_recovery_plan(store, intent)
    finally:
        store.__exit__(None, None, None)
