from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp import candidate_rollback_execution as execution
from ha_syncapp.candidate_rollback_execution import (
    CandidateRollbackExecutionError,
    execute_candidate_rollback_once,
)
from ha_syncapp.deployment_rollback import (
    RollbackBackupProof,
    RollbackRepositoryProof,
    authorize_deployment_rollback_once,
    load_deployment_rollback,
)
from test_core_health_window import START, TOKEN
from test_deployment_rollback import _failed


def _running(tmp_path, monkeypatch):
    chain, plan, finalization = _failed(tmp_path, monkeypatch)
    store = chain[0]
    monkeypatch.setattr(execution, "_load_plan", lambda *_args: plan)
    prepared = store.prepared_deployment(finalization.deployment_id)
    assert prepared is not None
    now = START + timedelta(seconds=309)
    store.enqueue_work("candidate_rollback", finalization.deployment_id, now=now)
    item = store.claim_work_kind("candidate_rollback", now=now)
    assert item is not None
    return store, plan, prepared, item, now


def _repository(prepared):
    return RollbackRepositoryProof(
        prepared.evidence.repository_id, True, prepared.evidence.baseline_sha
    )


def _backup(prepared):
    return RollbackBackupProof(
        prepared.evidence.backup_slug,
        "full",
        prepared.evidence.core_version,
        True,
        True,
    )


def test_authorized_rollback_handoff_completes_exact_work(tmp_path, monkeypatch) -> None:
    store, plan, prepared, item, now = _running(tmp_path, monkeypatch)
    reads: list[str] = []
    try:
        result = execute_candidate_rollback_once(
            store,
            item,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=lambda *_args: reads.append("repository") or _repository(prepared),
            backup_reader=lambda *_args: reads.append("backup") or _backup(prepared),
            now=now,
        )
        assert result.action == "rollback_authorized"
        assert result.status == "planned"
        assert result.work.status == "succeeded"
        assert result.replayed is False
        assert reads == ["repository", "backup"]
        saved = load_deployment_rollback(store, plan)
        assert saved is not None
        assert saved.deployment_id == item.work_key
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='deployment_rollback'"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.__exit__(None, None, None)


def test_authorized_handoff_replays_without_credentials_or_network(tmp_path, monkeypatch) -> None:
    store, plan, prepared, item, now = _running(tmp_path, monkeypatch)
    authorize_deployment_rollback_once(
        store,
        plan,
        github_token=TOKEN,
        supervisor_token=TOKEN,
        repository_reader=lambda *_args: _repository(prepared),
        backup_reader=lambda *_args: _backup(prepared),
        observed_at=now,
    )
    try:
        result = execute_candidate_rollback_once(
            store,
            item,
            github_token=None,
            supervisor_token=None,
            repository_reader=lambda *_args: pytest.fail("replay read repository"),
            backup_reader=lambda *_args: pytest.fail("replay read backup"),
            now=now + timedelta(seconds=1),
        )
        assert result.replayed is True
        assert result.work.status == "succeeded"
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("failed_reader", ["repository", "backup"])
def test_proof_transport_failure_is_transient(tmp_path, monkeypatch, failed_reader: str) -> None:
    store, _plan, prepared, item, now = _running(tmp_path, monkeypatch)

    def repository(*_args):
        if failed_reader == "repository":
            raise TimeoutError("secret")
        return _repository(prepared)

    def backup(*_args):
        if failed_reader == "backup":
            raise TimeoutError("secret")
        return _backup(prepared)

    try:
        with pytest.raises(CandidateRollbackExecutionError) as error:
            execute_candidate_rollback_once(
                store,
                item,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=repository,
                backup_reader=backup,
                now=now,
            )
        assert error.value.transient is True
        assert store._get_work(item.work_kind, item.work_key).status == "running"
    finally:
        store.__exit__(None, None, None)


def test_invalid_proof_is_deterministic(tmp_path, monkeypatch) -> None:
    store, _plan, prepared, item, now = _running(tmp_path, monkeypatch)
    try:
        with pytest.raises(CandidateRollbackExecutionError) as error:
            execute_candidate_rollback_once(
                store,
                item,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=lambda *_args: RollbackRepositoryProof(
                    prepared.evidence.repository_id, False, prepared.evidence.baseline_sha
                ),
                backup_reader=lambda *_args: pytest.fail("invalid repository read backup"),
                now=now,
            )
        assert error.value.transient is False
    finally:
        store.__exit__(None, None, None)


def test_invalid_work_identity_is_rejected_before_network(tmp_path, monkeypatch) -> None:
    store, _plan, _prepared, item, now = _running(tmp_path, monkeypatch)
    wrong = item.__class__(
        "candidate_promote",
        item.work_key,
        item.status,
        item.attempts,
        item.created_at,
        item.updated_at,
        item.next_attempt_at,
    )
    try:
        with pytest.raises(CandidateRollbackExecutionError) as error:
            execute_candidate_rollback_once(
                store,
                wrong,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=lambda *_args: pytest.fail("invalid work read repository"),
                backup_reader=lambda *_args: pytest.fail("invalid work read backup"),
                now=now,
            )
        assert error.value.transient is False
    finally:
        store.__exit__(None, None, None)
