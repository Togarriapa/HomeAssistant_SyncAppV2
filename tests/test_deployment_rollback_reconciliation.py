from __future__ import annotations

import json
from datetime import timedelta

import pytest
from ha_syncapp.deployment_rollback import (
    DeploymentRollbackError,
    SupervisorRestoreResponse,
    load_deployment_rollback,
    reconcile_deployment_restore_once,
    request_deployment_restore_once,
)
from test_core_health_window import START, TOKEN
from test_deployment_rollback_transport import _authorized


def _acknowledged(tmp_path, monkeypatch):
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
    return chain, plan, prepared


def _job(*, created, reference="backup-1", uuid="job-123", done=None, errors=None):
    return {
        "name": "backup_manager_full_restore",
        "reference": reference,
        "uuid": uuid,
        "progress": 50.0 if done is None else 100.0,
        "stage": "restore_home_assistant",
        "done": done,
        "errors": [] if errors is None else errors,
        "created": created.isoformat(),
        "extra": None,
        "child_jobs": [],
    }


def _response(data):
    return SupervisorRestoreResponse(
        200,
        "application/json",
        json.dumps({"result": "ok", "data": data}, separators=(",", ":")).encode(),
    )


def test_acknowledged_restore_job_in_progress_is_reconciled_read_only(tmp_path, monkeypatch):
    chain, plan, prepared = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    calls = []

    def transport(method, url, headers, body, timeout, limit):
        calls.append((method, url, headers, body, timeout, limit))
        return _response(
            _job(
                created=START + timedelta(seconds=310),
                reference=prepared.evidence.backup_slug,
            )
        )

    try:
        result = reconcile_deployment_restore_once(
            store,
            plan,
            supervisor_token=TOKEN,
            transport=transport,
            observed_at=START + timedelta(seconds=311),
        )
        assert result.status == "in_progress"
        assert result.replayed is False
        assert calls == [
            (
                "GET",
                "http://supervisor/jobs/job-123",
                {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"},
                b"",
                60.0,
                64 * 1024,
            )
        ]
        saved = load_deployment_rollback(store, plan)
        assert saved is not None
        assert saved.phase == "restore_acknowledged"
        assert saved.reconciliation_state == "in_progress"
        assert saved.restore_job_id == "job-123"
        assert saved.backup_slug == prepared.evidence.backup_slug
    finally:
        store.__exit__(None, None, None)


def test_successful_exact_job_advances_to_observation_and_replays_offline(tmp_path, monkeypatch):
    chain, plan, prepared = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    try:
        result = reconcile_deployment_restore_once(
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
        assert result.status == "restored"
        assert result.replayed is False
        saved = load_deployment_rollback(store, plan)
        assert saved is not None
        assert saved.phase == "observing"
        assert saved.reconciliation_state == "restored"

        replay = reconcile_deployment_restore_once(
            store,
            plan,
            supervisor_token=None,
            transport=lambda *_args: pytest.fail("restored replay used network"),
        )
        assert replay.status == "restored"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_uncertain_acknowledgement_discovers_one_exact_restore_job(tmp_path, monkeypatch):
    chain, plan, prepared, repository_reader, backup_reader = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(DeploymentRollbackError, match="outcome is uncertain"):
            request_deployment_restore_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=repository_reader,
                backup_reader=backup_reader,
                transport=lambda *_args: (_ for _ in ()).throw(TimeoutError("private")),
                requested_at=START + timedelta(seconds=310),
            )

        calls = []

        def transport(method, url, headers, body, timeout, limit):
            calls.append((method, url, body))
            job = _job(
                created=START + timedelta(seconds=310),
                uuid="recovered-job",
                done=True,
            )
            job["reference"] = prepared.evidence.backup_slug
            return _response({"ignore_conditions": [], "jobs": [job]})

        result = reconcile_deployment_restore_once(
            store,
            plan,
            supervisor_token=TOKEN,
            transport=transport,
            observed_at=START + timedelta(seconds=311),
        )
        assert result.status == "restored"
        assert calls == [("GET", "http://supervisor/jobs/info", b"")]
        saved = load_deployment_rollback(store, plan)
        assert saved is not None
        assert saved.phase == "observing"
        assert saved.restore_job_id == "recovered-job"
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("evidence", ["missing", "duplicate", "stale", "failed", "malformed"])
def test_uncertain_non_unique_or_invalid_job_evidence_is_durably_blocked(
    tmp_path, monkeypatch, evidence
):
    chain, plan, prepared, repository_reader, backup_reader = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(DeploymentRollbackError):
            request_deployment_restore_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=repository_reader,
                backup_reader=backup_reader,
                transport=lambda *_args: (_ for _ in ()).throw(TimeoutError()),
                requested_at=START + timedelta(seconds=310),
            )
        exact = _job(created=START + timedelta(seconds=310), uuid="found-job")
        exact["reference"] = prepared.evidence.backup_slug
        if evidence == "missing":
            jobs = []
        elif evidence == "duplicate":
            jobs = [exact, exact | {"uuid": "other-job"}]
        elif evidence == "stale":
            jobs = [exact | {"created": (START + timedelta(seconds=309)).isoformat()}]
        elif evidence == "failed":
            jobs = [exact | {"done": False, "errors": [{"message": "private"}]}]
        else:
            jobs = [exact | {"done": "yes"}]

        result = reconcile_deployment_restore_once(
            store,
            plan,
            supervisor_token=TOKEN,
            transport=lambda *_args: _response({"ignore_conditions": [], "jobs": jobs}),
            observed_at=START + timedelta(seconds=311),
        )
        assert result.status == "ambiguous"
        saved = load_deployment_rollback(store, plan)
        assert saved is not None
        assert saved.phase == "blocked"
        assert saved.reconciliation_state == "ambiguous"
        assert saved.block_reason == "ambiguous"

        replay = reconcile_deployment_restore_once(
            store,
            plan,
            supervisor_token=None,
            transport=lambda *_args: pytest.fail("blocked replay used network"),
        )
        assert replay.status == "ambiguous"
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_transient_reconciliation_failure_is_sanitized_and_preserves_state(tmp_path, monkeypatch):
    chain, plan, _prepared = _acknowledged(tmp_path, monkeypatch)
    store = chain[0]
    try:
        before = load_deployment_rollback(store, plan)
        with pytest.raises(DeploymentRollbackError, match="temporarily unavailable") as error:
            reconcile_deployment_restore_once(
                store,
                plan,
                supervisor_token=TOKEN,
                transport=lambda *_args: (_ for _ in ()).throw(
                    TimeoutError("private transport detail")
                ),
            )
        assert "private transport detail" not in str(error.value)
        assert load_deployment_rollback(store, plan) == before
    finally:
        store.__exit__(None, None, None)
