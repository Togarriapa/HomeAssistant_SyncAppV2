from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from ha_syncapp.config import DeployKeyAdministrativeRequest
from ha_syncapp.deploy_key import DeployKeyEnrollment
from ha_syncapp.deploy_key_access import DeployKeyAccessError, DeployKeyAccessProof
from ha_syncapp.deploy_key_administration import (
    DeployKeyAdministrationError,
    apply_deploy_key_administrative_request,
    load_deploy_key_administrative_receipt,
)
from ha_syncapp.state import StateStore

REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"
TARGET = "Owner/Home"
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
FINGERPRINT = "SHA256:" + "A" * 43
GENERATION_ID = "223e4567-e89b-42d3-a456-426614174000"


def _request(action: str = "generate") -> DeployKeyAdministrativeRequest:
    return DeployKeyAdministrativeRequest(REQUEST_ID, action)


def _enrollment() -> DeployKeyEnrollment:
    return DeployKeyEnrollment(
        "ssh-ed25519",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest homeassistant-syncapp-repo-b",
        FINGERPRINT,
        GENERATION_ID,
    )


def _proof() -> DeployKeyAccessProof:
    return DeployKeyAccessProof(TARGET, 123, FINGERPRINT, GENERATION_ID, 0, "a" * 64)


def test_generate_is_consumed_once_and_replays_only_safe_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def generate(path: Path) -> DeployKeyEnrollment:
        nonlocal calls
        calls += 1
        assert path == tmp_path / "key"
        return _enrollment()

    monkeypatch.setattr("ha_syncapp.deploy_key_administration.ensure_repo_b_deploy_key", generate)
    with StateStore(tmp_path) as store:
        first = apply_deploy_key_administrative_request(
            store,
            _request(),
            target=TARGET,
            repository_id=123,
            github_token="secret-sentinel",
            key_directory=tmp_path / "key",
            work_directory=tmp_path / "work",
            source=tmp_path / "source",
            snapshot_root=tmp_path / "snapshots",
            workspace_root=tmp_path / "workspaces",
            now=NOW,
        )
        replay = apply_deploy_key_administrative_request(
            store,
            _request(),
            target=TARGET,
            repository_id=123,
            github_token="secret-sentinel",
            key_directory=tmp_path / "key",
            work_directory=tmp_path / "work",
            source=tmp_path / "source",
            snapshot_root=tmp_path / "snapshots",
            workspace_root=tmp_path / "workspaces",
            now=NOW + timedelta(minutes=1),
        )

        receipt = load_deploy_key_administrative_receipt(store, REQUEST_ID, "generate")

    assert calls == 1
    assert first.outcome == "generated"
    assert first.replayed is False
    assert first.public_key == _enrollment().public_key
    assert replay == first.__class__(
        "generated", True, _enrollment().public_key, FINGERPRINT, GENERATION_ID
    )
    assert receipt is not None
    assert receipt.target_sha256 != TARGET
    assert "secret-sentinel" not in repr(receipt)


def test_transient_access_failure_uses_durable_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def test_access(*args: object, **kwargs: object) -> DeployKeyAccessProof:
        nonlocal calls
        calls += 1
        raise DeployKeyAccessError("temporary", transient=True)

    monkeypatch.setattr(
        "ha_syncapp.deploy_key_administration.test_repo_b_deploy_key_access", test_access
    )
    with StateStore(tmp_path) as store:
        first = apply_deploy_key_administrative_request(
            store,
            _request("test"),
            target=TARGET,
            repository_id=123,
            github_token="secret-sentinel",
            key_directory=tmp_path / "key",
            work_directory=tmp_path / "work",
            source=tmp_path / "source",
            snapshot_root=tmp_path / "snapshots",
            workspace_root=tmp_path / "workspaces",
            now=NOW,
        )
        deferred = apply_deploy_key_administrative_request(
            store,
            _request("test"),
            target=TARGET,
            repository_id=123,
            github_token="secret-sentinel",
            key_directory=tmp_path / "key",
            work_directory=tmp_path / "work",
            source=tmp_path / "source",
            snapshot_root=tmp_path / "snapshots",
            workspace_root=tmp_path / "workspaces",
            now=NOW + timedelta(seconds=30),
        )

    assert calls == 1
    assert first.outcome == "retry"
    assert deferred.outcome == "retry"
    assert deferred.replayed is True


def test_deterministic_failure_is_blocked_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def test_access(*args: object, **kwargs: object) -> DeployKeyAccessProof:
        nonlocal calls
        calls += 1
        raise DeployKeyAccessError("rejected", transient=False)

    monkeypatch.setattr(
        "ha_syncapp.deploy_key_administration.test_repo_b_deploy_key_access", test_access
    )
    with StateStore(tmp_path) as store:
        first = apply_deploy_key_administrative_request(
            store,
            _request("test"),
            target=TARGET,
            repository_id=123,
            github_token="secret-sentinel",
            key_directory=tmp_path / "key",
            work_directory=tmp_path / "work",
            source=tmp_path / "source",
            snapshot_root=tmp_path / "snapshots",
            workspace_root=tmp_path / "workspaces",
            now=NOW,
        )
        replay = apply_deploy_key_administrative_request(
            store,
            _request("test"),
            target=TARGET,
            repository_id=123,
            github_token="secret-sentinel",
            key_directory=tmp_path / "key",
            work_directory=tmp_path / "work",
            source=tmp_path / "source",
            snapshot_root=tmp_path / "snapshots",
            workspace_root=tmp_path / "workspaces",
            now=NOW + timedelta(days=1),
        )

    assert calls == 1
    assert first.outcome == "blocked"
    assert replay.replayed is True


def test_request_id_cannot_be_rebound_to_another_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ha_syncapp.deploy_key_administration.ensure_repo_b_deploy_key",
        lambda path: _enrollment(),
    )
    with StateStore(tmp_path) as store:
        apply_deploy_key_administrative_request(
            store,
            _request(),
            target=TARGET,
            repository_id=123,
            github_token="secret-sentinel",
            key_directory=tmp_path / "key",
            work_directory=tmp_path / "work",
            source=tmp_path / "source",
            snapshot_root=tmp_path / "snapshots",
            workspace_root=tmp_path / "workspaces",
            now=NOW,
        )
        with pytest.raises(DeployKeyAdministrationError):
            apply_deploy_key_administrative_request(
                store,
                _request(),
                target="Other/Home",
                repository_id=456,
                github_token="secret-sentinel",
                key_directory=tmp_path / "key",
                work_directory=tmp_path / "work",
                source=tmp_path / "source",
                snapshot_root=tmp_path / "snapshots",
                workspace_root=tmp_path / "workspaces",
                now=NOW,
            )


@pytest.mark.parametrize(
    ("action", "primitive", "expected"),
    [
        ("rotate_prepare", "prepare_repo_b_deploy_key_rotation", "prepared"),
        ("rotate_verify", "verify_repo_b_deploy_key_rotation", "verified"),
        ("rotate_activate", "activate_repo_b_deploy_key_rotation", "activated"),
    ],
)
def test_rotation_actions_dispatch_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    primitive: str,
    expected: str,
) -> None:
    calls = 0

    def rotate(*args: object, **kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            candidate_public_key=(
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest homeassistant-syncapp-repo-b"
                if action == "rotate_prepare"
                else None
            ),
            candidate_fingerprint=FINGERPRINT,
            active_fingerprint=FINGERPRINT,
            candidate_generation_id=GENERATION_ID,
            active_generation_id=GENERATION_ID,
        )

    monkeypatch.setattr(f"ha_syncapp.deploy_key_administration.{primitive}", rotate)
    with StateStore(tmp_path) as store:
        result = apply_deploy_key_administrative_request(
            store,
            _request(action),
            target=TARGET,
            repository_id=123,
            github_token="secret-sentinel",
            key_directory=tmp_path / "key",
            work_directory=tmp_path / "work",
            source=tmp_path / "source",
            snapshot_root=tmp_path / "snapshots",
            workspace_root=tmp_path / "workspaces",
            now=NOW,
        )

    assert calls == 1
    assert result.outcome == expected


def test_initialize_requires_fresh_empty_proof_then_executes_exact_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = _proof()
    order: list[str] = []
    monkeypatch.setattr(
        "ha_syncapp.deploy_key_administration.test_repo_b_deploy_key_access",
        lambda *args, **kwargs: order.append("test") or proof,
    )
    monkeypatch.setattr(
        "ha_syncapp.deploy_key_administration.read_repo_b_deploy_key_references",
        lambda *args, **kwargs: order.append("references") or SimpleNamespace(),
    )
    monkeypatch.setattr(
        "ha_syncapp.deploy_key_administration.authorize_repo_b_initialization",
        lambda *args, **kwargs: order.append("authorize") or SimpleNamespace(phase="authorized"),
    )
    monkeypatch.setattr(
        "ha_syncapp.deploy_key_administration.execute_authorized_repo_b_initialization",
        lambda *args, **kwargs: order.append("execute") or SimpleNamespace(phase="completed"),
    )

    with StateStore(tmp_path) as store:
        result = apply_deploy_key_administrative_request(
            store,
            _request("initialize"),
            target=TARGET,
            repository_id=123,
            github_token="secret-sentinel",
            key_directory=tmp_path / "key",
            work_directory=tmp_path / "work",
            source=tmp_path / "source",
            snapshot_root=tmp_path / "snapshots",
            workspace_root=tmp_path / "workspaces",
            now=NOW,
        )

    assert order == ["test", "references", "authorize", "execute"]
    assert result.outcome == "initialized"


def test_schema_37_migrates_operator_receipts_without_losing_state(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("runtime", "preserve-me", now=NOW)
        store._connection.execute("DROP TABLE deploy_key_administrative_request")
        store._connection.execute("PRAGMA user_version = 37")

    with StateStore(tmp_path) as migrated:
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (38,)
        assert migrated._get_work("runtime", "preserve-me").status == "pending"
        columns = tuple(
            row[1]
            for row in migrated._connection.execute(
                "PRAGMA table_info(deploy_key_administrative_request)"
            )
        )

    assert columns == (
        "request_id",
        "action",
        "target_sha256",
        "repository_id",
        "status",
        "outcome",
        "attempts",
        "next_attempt_at",
        "processed_at",
        "public_key",
        "fingerprint",
        "generation_id",
        "record_sha256",
    )
