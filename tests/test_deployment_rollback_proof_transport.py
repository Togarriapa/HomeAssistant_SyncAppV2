from __future__ import annotations

import json

import pytest
from ha_syncapp.deployment_rollback_transport import (
    DeploymentRollbackTransportError,
    SupervisorBackupProofResponse,
    read_rollback_backup_proof,
    read_rollback_repository_proof,
)
from ha_syncapp.github_repo import BranchHead


def test_repository_proof_reuses_exact_private_identity_and_main_head(monkeypatch):
    calls: list[tuple[object, ...]] = []

    def fetch(target, token, *, expected_id, branch):
        calls.append((target, token, expected_id, branch))
        return BranchHead(target, expected_id, branch, "a" * 40)

    monkeypatch.setattr("ha_syncapp.deployment_rollback_transport.fetch_trusted_branch_head", fetch)
    proof = read_rollback_repository_proof("Owner/Private-Home", "token", 123)

    assert proof.repository_id == 123
    assert proof.private is True
    assert proof.main_sha == "a" * 40
    assert calls == [("Owner/Private-Home", "token", 123, "main")]


def test_backup_proof_reads_one_bounded_exact_supervisor_backup():
    calls: list[tuple[object, ...]] = []

    def transport(method, url, headers, body, timeout, maximum):
        calls.append((method, url, headers, body, timeout, maximum))
        return SupervisorBackupProofResponse(
            200,
            "application/json; charset=utf-8",
            json.dumps(
                {
                    "slug": "backup_123",
                    "type": "full",
                    "homeassistant": "2026.9.1",
                    "content": {"homeassistant": True},
                }
            ).encode(),
        )

    proof = read_rollback_backup_proof("backup_123", "token", transport=transport)

    assert proof.slug == "backup_123"
    assert proof.backup_type == "full"
    assert proof.homeassistant_version == "2026.9.1"
    assert proof.includes_homeassistant is True
    assert proof.restorable is True
    assert calls[0][0:2] == (
        "GET",
        "http://supervisor/backups/backup_123/info",
    )
    assert calls[0][2]["Authorization"] == "Bearer token"
    assert calls[0][3] == b""


@pytest.mark.parametrize(
    "payload",
    [
        {"slug": "wrong", "type": "full", "homeassistant": "2026.9.1"},
        {"slug": "backup_123", "type": "partial", "homeassistant": "2026.9.1"},
        {"slug": "backup_123", "type": "full", "homeassistant": "invalid"},
        {
            "slug": "backup_123",
            "type": "full",
            "homeassistant": "2026.9.1",
            "content": {"homeassistant": False},
        },
    ],
)
def test_backup_proof_rejects_wrong_or_unrestorable_evidence(payload):
    def transport(*_args):
        return SupervisorBackupProofResponse(200, "application/json", json.dumps(payload).encode())

    with pytest.raises(DeploymentRollbackTransportError, match="invalid"):
        read_rollback_backup_proof("backup_123", "token", transport=transport)
