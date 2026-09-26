from __future__ import annotations

from datetime import timedelta

from ha_syncapp import deployment_rollback_retrigger as retrigger
from ha_syncapp.deployment_rollback import RollbackRestoreResult
from test_core_health_window import START, TOKEN
from test_deployment_rollback_recovery_authority import _authorized


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
