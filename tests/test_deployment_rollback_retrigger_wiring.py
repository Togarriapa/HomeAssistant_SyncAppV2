from __future__ import annotations

from datetime import timedelta

from ha_syncapp import deployment_rollback_retrigger as retrigger
from ha_syncapp.deploy_key_access import DeployKeyAccessProof
from ha_syncapp.deploy_key_rollback_authority import DeployKeyRollbackRepositoryAuthority
from ha_syncapp.deployment_rollback import RollbackRestoreResult
from test_core_health_window import START, TOKEN
from test_deployment_rollback_recovery_authority import _authorized


def _deploy_key_authority(tmp_path) -> DeployKeyRollbackRepositoryAuthority:
    return DeployKeyRollbackRepositoryAuthority(
        DeployKeyAccessProof(
            "owner/private-repo",
            12345,
            "SHA256:" + "A" * 43,
            "123e4567-e89b-42d3-a456-426614174000",
            1,
            "a" * 64,
        ),
        tmp_path / "key",
        tmp_path / "rollback-access",
    )


def test_reconciliation_reconstructs_exact_plan_before_delegating(tmp_path, monkeypatch):
    store, plan, rollback = _authorized(tmp_path, monkeypatch)
    seen: list[object] = []

    def reconcile(candidate_store, candidate_plan, **kwargs):
        seen.extend((candidate_store, candidate_plan, kwargs))
        return RollbackRestoreResult("in_progress", False)

    monkeypatch.setattr(retrigger, "reconcile_deployment_restore_once", reconcile, raising=False)
    try:
        result = retrigger.reconcile_pending_rollback(
            store,
            rollback,
            supervisor_token=TOKEN,
            observed_at=START + timedelta(seconds=310),
        )
    finally:
        store.__exit__(None, None, None)

    assert result == RollbackRestoreResult("in_progress", False)
    assert seen[0] is store
    assert seen[1] == plan
    assert seen[2] == {
        "supervisor_token": TOKEN,
        "observed_at": START + timedelta(seconds=310),
    }


def test_restore_execution_reconstructs_plan_and_uses_injected_proofs(tmp_path, monkeypatch):
    store, plan, rollback = _authorized(tmp_path, monkeypatch)
    repository_reader = object()
    backup_reader = object()
    seen: list[object] = []

    def request(candidate_store, candidate_plan, **kwargs):
        seen.extend((candidate_store, candidate_plan, kwargs))
        return RollbackRestoreResult("restore_acknowledged", False)

    monkeypatch.setattr(retrigger, "request_deployment_restore_once", request, raising=False)
    try:
        result = retrigger.execute_rollback_restore(
            store,
            rollback,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=repository_reader,
            backup_reader=backup_reader,
            attempted_at=START + timedelta(seconds=310),
        )
    finally:
        store.__exit__(None, None, None)

    assert result == RollbackRestoreResult("restore_acknowledged", False)
    assert seen[0] is store
    assert seen[1] == plan
    assert seen[2] == {
        "github_token": TOKEN,
        "supervisor_token": TOKEN,
        "repository_reader": repository_reader,
        "backup_reader": backup_reader,
        "requested_at": START + timedelta(seconds=310),
    }


def test_observation_completion_reconstructs_plan_before_health_proof(tmp_path, monkeypatch):
    store, plan, rollback = _authorized(tmp_path, monkeypatch)
    repository_reader = object()
    seen: list[object] = []

    def complete(candidate_store, candidate_plan, **kwargs):
        seen.extend((candidate_store, candidate_plan, kwargs))
        return RollbackRestoreResult("completed", False)

    monkeypatch.setattr(retrigger, "complete_deployment_rollback_once", complete, raising=False)
    try:
        result = retrigger.complete_rollback_observation(
            store,
            rollback,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=repository_reader,
            observed_at=START + timedelta(seconds=310),
        )
    finally:
        store.__exit__(None, None, None)

    assert result == RollbackRestoreResult("completed", False)
    assert seen[0] is store
    assert seen[1] == plan
    assert seen[2] == {
        "github_token": TOKEN,
        "supervisor_token": TOKEN,
        "repository_reader": repository_reader,
        "observed_at": START + timedelta(seconds=310),
    }


def test_restore_and_observation_forward_same_deploy_key_authority(tmp_path, monkeypatch):
    store, _plan, rollback = _authorized(tmp_path, monkeypatch)
    authority = _deploy_key_authority(tmp_path)
    observed: list[tuple[str, dict[str, object]]] = []

    def request(_store, _plan, **kwargs):
        observed.append(("restore", kwargs))
        return RollbackRestoreResult("restore_acknowledged", False)

    def complete(_store, _plan, **kwargs):
        observed.append(("observe", kwargs))
        return RollbackRestoreResult("completed", False)

    monkeypatch.setattr(retrigger, "request_deployment_restore_once", request)
    monkeypatch.setattr(retrigger, "complete_deployment_rollback_once", complete)
    try:
        retrigger.execute_rollback_restore(
            store,
            rollback,
            github_token=None,
            supervisor_token=TOKEN,
            repository_reader=None,
            repository_authority=authority,
            backup_reader=object(),
            attempted_at=START + timedelta(seconds=310),
        )
        retrigger.complete_rollback_observation(
            store,
            rollback,
            github_token=None,
            supervisor_token=TOKEN,
            repository_reader=None,
            repository_authority=authority,
            observed_at=START + timedelta(seconds=311),
        )
    finally:
        store.__exit__(None, None, None)

    assert observed[0][1]["repository_authority"] is authority
    assert observed[0][1]["github_token"] is None
    assert observed[0][1]["supervisor_token"] == TOKEN
    assert observed[1][1]["repository_authority"] is authority
    assert observed[1][1]["github_token"] is None
    assert observed[1][1]["supervisor_token"] == TOKEN
