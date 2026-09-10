from dataclasses import replace

import ha_syncapp.candidate_backup as backup
import ha_syncapp.candidate_semantics as semantic
import pytest
from semantic_fixtures import candidate_inputs


def _inputs_and_semantic(tmp_path, monkeypatch):
    inputs = candidate_inputs(tmp_path, {"configuration.yaml": b"homeassistant:\n"})
    monkeypatch.setattr(semantic.os, "chown", lambda *_args: None)
    monkeypatch.setattr(semantic, "_run_validator", lambda *_args: None)
    return inputs, semantic.validate_candidate_semantics(*inputs)


def _json_response(payload, status=200):
    import json

    return backup.SupervisorBackupResponse(
        status=status,
        content_type="application/json",
        body=json.dumps(payload).encode(),
    )


def test_success_creates_full_backup_and_binds_exact_candidate(tmp_path, monkeypatch):
    inputs, authorization = _inputs_and_semantic(tmp_path, monkeypatch)
    calls = []

    def transport(method, url, headers, body, timeout, limit):
        calls.append((method, url, headers, body, timeout, limit))
        if method == "POST":
            return _json_response({"slug": "abc123"})
        return _json_response(
            {
                "slug": "abc123",
                "type": "full",
                "homeassistant": "2026.9.1",
                "content": {"homeassistant": True},
            }
        )

    result = backup.create_candidate_backup(
        authorization, *inputs, token="secret-token", transport=transport
    )

    assert result.backup_slug == "abc123"
    assert result.repository_id == authorization.repository_id
    assert result.candidate_sha == authorization.candidate_sha
    assert result.stage_manifest_sha256 == authorization.stage_manifest_sha256
    assert result.runtime_sha256 == authorization.runtime_sha256
    assert result.risk_level == authorization.risk_level
    assert result.core_version == authorization.core_version
    assert [call[0] for call in calls] == ["POST", "GET"]
    assert calls[0][1] == "http://supervisor/backups/new/full"
    assert calls[1][1] == "http://supervisor/backups/abc123/info"
    assert calls[0][2]["Authorization"] == "Bearer secret-token"
    assert b'"background":false' in calls[0][3]


def test_invalid_semantic_evidence_never_calls_supervisor(tmp_path, monkeypatch):
    inputs, authorization = _inputs_and_semantic(tmp_path, monkeypatch)
    changed = replace(authorization, candidate_sha="c" * 40)

    with pytest.raises(backup.CandidateBackupError, match="semantic"):
        backup.create_candidate_backup(
            changed,
            *inputs,
            token="secret-token",
            transport=lambda *_args: pytest.fail("must not call Supervisor"),
        )


def test_missing_token_fails_before_backup_request(tmp_path, monkeypatch):
    inputs, authorization = _inputs_and_semantic(tmp_path, monkeypatch)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    with pytest.raises(backup.CandidateBackupError, match="credential"):
        backup.create_candidate_backup(
            authorization,
            *inputs,
            transport=lambda *_args: pytest.fail("must not call Supervisor"),
        )


@pytest.mark.parametrize(
    "create_payload",
    [
        {},
        {"slug": "abc123", "job_id": "unexpected"},
        {"slug": "../escape"},
        {"slug": ""},
    ],
)
def test_ambiguous_or_unsafe_creation_response_is_blocked(tmp_path, monkeypatch, create_payload):
    inputs, authorization = _inputs_and_semantic(tmp_path, monkeypatch)

    def transport(*_args):
        return _json_response(create_payload)

    with pytest.raises(backup.CandidateBackupError):
        backup.create_candidate_backup(
            authorization, *inputs, token="secret-token", transport=transport
        )


@pytest.mark.parametrize(
    "info",
    [
        {"slug": "other", "type": "full", "homeassistant": "2026.9.1"},
        {"slug": "abc123", "type": "partial", "homeassistant": "2026.9.1"},
        {"slug": "abc123", "type": "full", "homeassistant": "2026.9.0"},
        {
            "slug": "abc123",
            "type": "full",
            "homeassistant": "2026.9.1",
            "content": {"homeassistant": False},
        },
    ],
)
def test_backup_info_must_verify_exact_recoverable_home_assistant_backup(
    tmp_path, monkeypatch, info
):
    inputs, authorization = _inputs_and_semantic(tmp_path, monkeypatch)

    def transport(method, *_args):
        if method == "POST":
            return _json_response({"slug": "abc123"})
        return _json_response(info)

    with pytest.raises(backup.CandidateBackupError):
        backup.create_candidate_backup(
            authorization, *inputs, token="secret-token", transport=transport
        )


def test_transport_exception_is_sanitized(tmp_path, monkeypatch):
    inputs, authorization = _inputs_and_semantic(tmp_path, monkeypatch)

    def transport(*_args):
        raise OSError("secret-token candidate-content-canary")

    with pytest.raises(backup.CandidateBackupError) as caught:
        backup.create_candidate_backup(
            authorization, *inputs, token="secret-token", transport=transport
        )

    assert "secret-token" not in str(caught.value)
    assert "candidate-content-canary" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_semantic_drift_after_backup_discards_success(tmp_path, monkeypatch):
    inputs, authorization = _inputs_and_semantic(tmp_path, monkeypatch)

    def transport(method, *_args):
        if method == "POST":
            return _json_response({"slug": "abc123"})
        inputs[6].manifest["core_config"]["version"] = "2026.9.2"
        return _json_response({"slug": "abc123", "type": "full", "homeassistant": "2026.9.1"})

    with pytest.raises(backup.CandidateBackupError, match="semantic"):
        backup.create_candidate_backup(
            authorization, *inputs, token="secret-token", transport=transport
        )


def test_verified_backup_evidence_is_exactly_retrievable_after_restart(tmp_path, monkeypatch):
    from uuid import uuid4

    from ha_syncapp.state import StateStore

    inputs, authorization = _inputs_and_semantic(tmp_path, monkeypatch)

    def transport(method, *_args):
        if method == "POST":
            return _json_response({"slug": "abc123"})
        return _json_response({"slug": "abc123", "type": "full", "homeassistant": "2026.9.1"})

    evidence = backup.create_candidate_backup(
        authorization, *inputs, token="secret-token", transport=transport
    )
    data = tmp_path / "prepared-state"
    data.mkdir()
    deployment_id = str(uuid4())
    with StateStore(data) as store:
        store.bind_repository(evidence.target, evidence.repository_id)
        recorded = store.record_prepared_deployment(deployment_id, evidence)
    with StateStore(data) as store:
        restored = store.prepared_deployment(deployment_id)
        assert restored == recorded
        assert restored.evidence == evidence
    assert b"secret-token" not in (data / "syncapp/state.sqlite3").read_bytes()
