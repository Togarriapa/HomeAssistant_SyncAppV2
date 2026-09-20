from __future__ import annotations

import json
from datetime import timedelta

import pytest
from ha_syncapp.core_health_observation import CoreHealthResponse
from ha_syncapp.deployment_finalization import is_candidate_blocked_by_finalization
from ha_syncapp.deployment_rollback import (
    DeploymentRollbackError,
    RollbackRepositoryProof,
    SupervisorRestoreResponse,
    complete_deployment_rollback_once,
    load_deployment_rollback,
    reconcile_deployment_restore_once,
    request_deployment_restore_once,
)
from ha_syncapp.supervisor_health_observation import SupervisorHealthResponse
from test_core_health_window import START, TOKEN
from test_deployment_rollback_reconciliation import _job, _response
from test_deployment_rollback_transport import _authorized


def _observing(tmp_path, monkeypatch):
    chain, plan, prepared, repository_reader, backup_reader = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    request_deployment_restore_once(
        store,
        plan,
        github_token=TOKEN,
        supervisor_token=TOKEN,
        repository_reader=repository_reader,
        backup_reader=backup_reader,
        transport=lambda *_args: SupervisorRestoreResponse(
            200,
            "application/json",
            b'{"result":"ok","data":{"job_id":"job-123"}}',
        ),
        requested_at=START + timedelta(seconds=310),
    )
    reconcile_deployment_restore_once(
        store,
        plan,
        supervisor_token=TOKEN,
        transport=lambda *_args: _response(
            _job(
                created=START + timedelta(seconds=310),
                reference=prepared.evidence.backup_slug,
                done=True,
            )
        ),
        observed_at=START + timedelta(seconds=311),
    )
    return chain, plan, prepared


def test_completion_requires_exact_baseline_core_and_supervisor_health(tmp_path, monkeypatch):
    chain, plan, prepared = _observing(tmp_path, monkeypatch)
    store = chain[0]
    calls = []

    def repository_reader(target, token, repository_id):
        calls.append(("repository", target, token, repository_id))
        return RollbackRepositoryProof(repository_id, True, prepared.evidence.baseline_sha)

    def core_transport(method, url, headers, timeout, limit):
        calls.append(("core", method, url, headers, timeout, limit))
        return CoreHealthResponse(200, "application/json", b'{"message":"API running."}')

    def supervisor_transport(method, url, headers, timeout, limit):
        calls.append(("supervisor", method, url, headers, timeout, limit))
        return SupervisorHealthResponse(
            200,
            "application/json",
            json.dumps(
                {"result": "ok", "data": {"healthy": True, "supported": True}},
                separators=(",", ":"),
            ).encode(),
        )

    try:
        result = complete_deployment_rollback_once(
            store,
            plan,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=repository_reader,
            core_transport=core_transport,
            supervisor_transport=supervisor_transport,
            observed_at=START + timedelta(seconds=312),
        )
        assert result.status == "completed"
        assert result.replayed is False
        assert [call[0] for call in calls] == ["repository", "core", "supervisor"]
        saved = load_deployment_rollback(store, plan)
        assert saved is not None
        assert saved.phase == "completed"
        assert saved.reconciliation_state == "restored"
        assert saved.updated_at == START + timedelta(seconds=312)
        assert is_candidate_blocked_by_finalization(store, plan, prepared.evidence.candidate_sha)
    finally:
        store.__exit__(None, None, None)


def test_completed_replay_is_credential_and_network_free(tmp_path, monkeypatch):
    chain, plan, prepared = _observing(tmp_path, monkeypatch)
    store = chain[0]
    try:
        complete_deployment_rollback_once(
            store,
            plan,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=lambda *_args: RollbackRepositoryProof(
                prepared.evidence.repository_id, True, prepared.evidence.baseline_sha
            ),
            core_transport=lambda *_args: CoreHealthResponse(
                200, "application/json", b'{"message":"API running."}'
            ),
            supervisor_transport=lambda *_args: SupervisorHealthResponse(
                200,
                "application/json",
                b'{"result":"ok","data":{"healthy":true,"supported":true}}',
            ),
            observed_at=START + timedelta(seconds=312),
        )
        replay = complete_deployment_rollback_once(
            store,
            plan,
            github_token=None,
            supervisor_token=None,
            repository_reader=lambda *_args: pytest.fail("replay read repository"),
            core_transport=lambda *_args: pytest.fail("replay read Core"),
            supervisor_transport=lambda *_args: pytest.fail("replay read Supervisor"),
        )
        assert replay.status == "completed"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("unhealthy", ["core", "supervisor"])
def test_unhealthy_or_malformed_probe_never_completes(tmp_path, monkeypatch, unhealthy):
    chain, plan, prepared = _observing(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(DeploymentRollbackError, match="health is unavailable"):
            complete_deployment_rollback_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=lambda *_args: RollbackRepositoryProof(
                    prepared.evidence.repository_id, True, prepared.evidence.baseline_sha
                ),
                core_transport=lambda *_args: CoreHealthResponse(
                    503 if unhealthy == "core" else 200,
                    "application/json",
                    b'{"message":"API running."}',
                ),
                supervisor_transport=lambda *_args: SupervisorHealthResponse(
                    200,
                    "application/json",
                    (
                        b'{"result":"ok","data":{"healthy":false,"supported":true}}'
                        if unhealthy == "supervisor"
                        else b'{"result":"ok","data":{"healthy":true,"supported":true}}'
                    ),
                ),
            )
        saved = load_deployment_rollback(store, plan)
        assert saved is not None and saved.phase == "observing"
    finally:
        store.__exit__(None, None, None)


def test_baseline_divergence_blocks_completion_before_health_reads(tmp_path, monkeypatch):
    chain, plan, prepared = _observing(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = complete_deployment_rollback_once(
            store,
            plan,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=lambda *_args: RollbackRepositoryProof(
                prepared.evidence.repository_id, True, "f" * 40
            ),
            core_transport=lambda *_args: pytest.fail("divergence read Core"),
            supervisor_transport=lambda *_args: pytest.fail("divergence read Supervisor"),
            observed_at=START + timedelta(seconds=312),
        )
        assert result.status == "blocked"
        saved = load_deployment_rollback(store, plan)
        assert saved is not None
        assert saved.phase == "blocked"
        assert saved.block_reason == "repository_divergence"
    finally:
        store.__exit__(None, None, None)


def test_health_completion_persistence_failure_is_sanitized(tmp_path, monkeypatch):
    chain, plan, prepared = _observing(tmp_path, monkeypatch)
    store = chain[0]
    store._connection.execute(
        "CREATE TRIGGER reject_rollback_health BEFORE UPDATE ON deployment_rollback "
        "BEGIN SELECT RAISE(ABORT, 'private-storage-detail'); END"
    )
    try:
        with pytest.raises(DeploymentRollbackError, match="state is invalid") as error:
            complete_deployment_rollback_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=lambda *_args: RollbackRepositoryProof(
                    prepared.evidence.repository_id, True, prepared.evidence.baseline_sha
                ),
                core_transport=lambda *_args: CoreHealthResponse(
                    200, "application/json", b'{"message":"API running."}'
                ),
                supervisor_transport=lambda *_args: SupervisorHealthResponse(
                    200,
                    "application/json",
                    b'{"result":"ok","data":{"healthy":true,"supported":true}}',
                ),
                observed_at=START + timedelta(seconds=312),
            )
        assert "private-storage-detail" not in str(error.value)
        saved = load_deployment_rollback(store, plan)
        assert saved is not None and saved.phase == "observing"
    finally:
        store.__exit__(None, None, None)
