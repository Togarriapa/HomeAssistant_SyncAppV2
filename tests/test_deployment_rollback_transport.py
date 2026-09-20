from __future__ import annotations

import json
from datetime import timedelta

import pytest
from ha_syncapp.deployment_rollback import (
    DeploymentRollbackError,
    RollbackBackupProof,
    RollbackRepositoryProof,
    SupervisorRestoreResponse,
    authorize_deployment_rollback_once,
    load_deployment_rollback,
    request_deployment_restore_once,
)
from test_core_health_window import START, TOKEN
from test_deployment_rollback import _failed


def _authorized(tmp_path, monkeypatch):
    chain, plan, finalization = _failed(tmp_path, monkeypatch)
    store = chain[0]
    prepared = store.prepared_deployment(finalization.deployment_id)
    assert prepared is not None

    def repository_reader(*_args):
        return RollbackRepositoryProof(
            prepared.evidence.repository_id,
            True,
            prepared.evidence.baseline_sha,
        )

    def backup_reader(*_args):
        return RollbackBackupProof(
            prepared.evidence.backup_slug,
            "full",
            prepared.evidence.core_version,
            True,
            True,
        )

    authorize_deployment_rollback_once(
        store,
        plan,
        github_token=TOKEN,
        supervisor_token=TOKEN,
        repository_reader=repository_reader,
        backup_reader=backup_reader,
        observed_at=START + timedelta(seconds=309),
    )
    return chain, plan, prepared, repository_reader, backup_reader


def test_restore_is_journaled_before_exact_bounded_supervisor_request(tmp_path, monkeypatch):
    chain, plan, prepared, repository_reader, backup_reader = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    calls = []

    def transport(method, url, headers, body, timeout, limit):
        intent = load_deployment_rollback(store, plan)
        assert intent is not None
        assert intent.phase == "restore_started"
        assert intent.attempt_count == 1
        calls.append((method, url, headers, body, timeout, limit))
        return SupervisorRestoreResponse(
            200,
            "application/json",
            b'{"result":"ok","data":{"job_id":"job-123"}}',
        )

    try:
        result = request_deployment_restore_once(
            store,
            plan,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=repository_reader,
            backup_reader=backup_reader,
            transport=transport,
            requested_at=START + timedelta(seconds=310),
        )
        assert result.status == "restore_acknowledged"
        assert result.replayed is False
        assert calls == [
            (
                "POST",
                f"http://supervisor/backups/{prepared.evidence.backup_slug}/restore/full",
                {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                },
                json.dumps({"background": True}, separators=(",", ":")).encode(),
                60.0,
                64 * 1024,
            )
        ]
        saved = load_deployment_rollback(store, plan)
        assert saved is not None
        assert saved.phase == "restore_acknowledged"
        assert saved.restore_job_id == "job-123"
    finally:
        store.__exit__(None, None, None)


def test_acknowledged_replay_is_credential_and_mutation_free(tmp_path, monkeypatch):
    chain, plan, _prepared, repository_reader, backup_reader = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        return SupervisorRestoreResponse(
            200,
            "application/json",
            b'{"result":"ok","data":{"job_id":"job-123"}}',
        )

    try:
        request_deployment_restore_once(
            store,
            plan,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=repository_reader,
            backup_reader=backup_reader,
            transport=transport,
            requested_at=START + timedelta(seconds=310),
        )
        replay = request_deployment_restore_once(
            store,
            plan,
            github_token=None,
            supervisor_token=None,
            repository_reader=lambda *_args: pytest.fail("replay read repository"),
            backup_reader=lambda *_args: pytest.fail("replay read backup"),
            transport=lambda *_args: pytest.fail("replay restored twice"),
        )
        assert replay.status == "restore_acknowledged"
        assert replay.replayed is True
        assert calls == 1
    finally:
        store.__exit__(None, None, None)


def test_timeout_becomes_uncertain_and_never_blindly_restores_again(tmp_path, monkeypatch):
    chain, plan, _prepared, repository_reader, backup_reader = _authorized(tmp_path, monkeypatch)
    store = chain[0]

    def timeout(*_args):
        raise TimeoutError("secret transport detail")

    try:
        with pytest.raises(DeploymentRollbackError, match="outcome is uncertain") as error:
            request_deployment_restore_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=repository_reader,
                backup_reader=backup_reader,
                transport=timeout,
                requested_at=START + timedelta(seconds=310),
            )
        assert "secret transport detail" not in str(error.value)
        saved = load_deployment_rollback(store, plan)
        assert saved is not None and saved.phase == "uncertain"

        replay = request_deployment_restore_once(
            store,
            plan,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=lambda *_args: pytest.fail("uncertain replay read repository"),
            backup_reader=lambda *_args: pytest.fail("uncertain replay read backup"),
            transport=lambda *_args: pytest.fail("uncertain replay restored twice"),
        )
        assert replay.status == "reconciliation_required"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_definite_pre_mutation_rejection_is_durably_blocked(tmp_path, monkeypatch):
    chain, plan, _prepared, repository_reader, backup_reader = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = request_deployment_restore_once(
            store,
            plan,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=repository_reader,
            backup_reader=backup_reader,
            transport=lambda *_args: SupervisorRestoreResponse(
                400,
                "application/json",
                b'{"result":"error","message":"private server detail"}',
            ),
            requested_at=START + timedelta(seconds=310),
        )
        assert result.status == "blocked"
        saved = load_deployment_rollback(store, plan)
        assert saved is not None
        assert saved.phase == "blocked"
        assert saved.block_reason == "restore_rejected"
        replay = request_deployment_restore_once(
            store,
            plan,
            github_token=None,
            supervisor_token=None,
            repository_reader=lambda *_args: pytest.fail("blocked replay read repository"),
            backup_reader=lambda *_args: pytest.fail("blocked replay read backup"),
            transport=lambda *_args: pytest.fail("blocked replay restored"),
        )
        assert replay.status == "blocked"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_journal_persistence_failure_prevents_restore(tmp_path, monkeypatch):
    chain, plan, _prepared, repository_reader, backup_reader = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    store._connection.execute(
        "CREATE TRIGGER reject_restore_start BEFORE UPDATE ON deployment_rollback "
        "BEGIN SELECT RAISE(ABORT, 'secret-storage-detail'); END"
    )
    try:
        with pytest.raises(DeploymentRollbackError, match="state is invalid") as error:
            request_deployment_restore_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=repository_reader,
                backup_reader=backup_reader,
                transport=lambda *_args: pytest.fail("restore preceded durable journal"),
                requested_at=START + timedelta(seconds=310),
            )
        assert "secret-storage-detail" not in str(error.value)
        saved = load_deployment_rollback(store, plan)
        assert saved is not None and saved.phase == "planned"
    finally:
        store.__exit__(None, None, None)
