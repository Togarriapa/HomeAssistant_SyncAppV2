from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp import deploy_key_rollback_authority as authority_module
from ha_syncapp.config import Config
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from ha_syncapp.deployment_rollback import (
    DeploymentRollbackError,
    RollbackBackupProof,
    authorize_deployment_rollback_once,
)
from ha_syncapp.state import StateStore
from test_core_health_window import START, TOKEN
from test_deployment_rollback import _failed

TARGET = "Owner/Home"
REPOSITORY_ID = 123
BASELINE = "a" * 40
NOW = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)
PROOF = DeployKeyAccessProof(
    target=TARGET,
    repository_id=REPOSITORY_ID,
    key_fingerprint="SHA256:" + "A" * 43,
    generation_id="123e4567-e89b-42d3-a456-426614174000",
    ref_count=2,
    observation_sha256="c" * 64,
)


def _authority(tmp_path: Path) -> authority_module.DeployKeyRollbackRepositoryAuthority:
    return authority_module.DeployKeyRollbackRepositoryAuthority(
        PROOF,
        tmp_path / "key",
        tmp_path / "access",
    )


def _snapshot(*references: DeployKeyReference) -> DeployKeyReferenceSnapshot:
    return DeployKeyReferenceSnapshot(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        key_fingerprint=PROOF.key_fingerprint,
        generation_id=PROOF.generation_id,
        references=tuple(references),
        observation_sha256="d" * 64,
    )


def test_authority_reads_exact_main_from_proof_bound_reference_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[object, ...]] = []

    def read(*args: object, **kwargs: object) -> DeployKeyReferenceSnapshot:
        observed.append(args)
        return _snapshot(
            DeployKeyReference("refs/heads/main", BASELINE),
            DeployKeyReference("refs/tags/syncapp-known-good", "b" * 40),
        )

    monkeypatch.setattr(authority_module, "read_repo_b_deploy_key_references", read)
    authority = _authority(tmp_path)

    result = authority.read(TARGET, REPOSITORY_ID)

    assert result.repository_id == REPOSITORY_ID
    assert result.private is True
    assert result.main_sha == BASELINE
    assert observed[0][:4] == (PROOF, TARGET, REPOSITORY_ID, tmp_path / "key")
    assert str(tmp_path) not in repr(authority)


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(),
        _snapshot(
            DeployKeyReference("refs/heads/main", BASELINE),
            DeployKeyReference("refs/heads/main", "b" * 40),
        ),
        DeployKeyReferenceSnapshot(
            TARGET,
            999,
            PROOF.key_fingerprint,
            PROOF.generation_id,
            (DeployKeyReference("refs/heads/main", BASELINE),),
            "d" * 64,
        ),
    ],
)
def test_authority_rejects_missing_duplicate_or_wrong_identity_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: DeployKeyReferenceSnapshot,
) -> None:
    monkeypatch.setattr(
        authority_module,
        "read_repo_b_deploy_key_references",
        lambda *_args, **_kwargs: snapshot,
    )

    with pytest.raises(authority_module.DeployKeyRollbackRepositoryAuthorityError):
        _authority(tmp_path).read(TARGET, REPOSITORY_ID)


@pytest.mark.parametrize("transient", [False, True])
def test_authority_sanitizes_transport_failure_and_preserves_retry_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient: bool
) -> None:
    monkeypatch.setattr(
        authority_module,
        "read_repo_b_deploy_key_references",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DeployKeyAccessError("private-key secret detail", transient=transient)
        ),
    )

    with pytest.raises(
        authority_module.DeployKeyRollbackRepositoryAuthorityError
    ) as caught:
        _authority(tmp_path).read(TARGET, REPOSITORY_ID)

    assert caught.value.transient is transient
    assert "secret" not in str(caught.value)


def test_authorization_uses_deploy_key_authority_without_github_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain, plan, finalization = _failed(tmp_path, monkeypatch)
    store = chain[0]
    prepared = store.prepared_deployment(finalization.deployment_id)
    assert prepared is not None
    authority = _authority(tmp_path)
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        authority_module.DeployKeyRollbackRepositoryAuthority,
        "read",
        lambda _self, target, repository_id: calls.append((target, repository_id))
        or authority_module.RollbackRepositoryProof(
            repository_id, True, prepared.evidence.baseline_sha
        ),
    )

    try:
        result = authorize_deployment_rollback_once(
            store,
            plan,
            github_token=None,
            supervisor_token=TOKEN,
            repository_reader=None,
            repository_authority=authority,
            backup_reader=lambda slug, _token: RollbackBackupProof(
                slug, "full", prepared.evidence.core_version, True, True
            ),
            observed_at=START + timedelta(seconds=309),
        )
        assert result.status == "planned"
        assert calls == [(prepared.evidence.target, prepared.evidence.repository_id)]

        replay = authorize_deployment_rollback_once(
            store,
            plan,
            github_token=None,
            supervisor_token=None,
            repository_reader=None,
            repository_authority=authority,
            backup_reader=lambda *_args: pytest.fail("replay must not use network"),
        )
        assert replay.replayed is True
        assert calls == [(prepared.evidence.target, prepared.evidence.repository_id)]
    finally:
        store.__exit__(None, None, None)


def test_authorization_rejects_mixed_token_and_deploy_key_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain, plan, _finalization = _failed(tmp_path, monkeypatch)
    store = chain[0]
    try:
        with pytest.raises(DeploymentRollbackError, match="exactly one"):
            authorize_deployment_rollback_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=None,
                repository_authority=_authority(tmp_path),
                backup_reader=lambda *_args: pytest.fail("mixed authority read"),
            )
    finally:
        store.__exit__(None, None, None)


def test_service_builds_rollback_authority_only_after_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    (data / "syncapp" / "repo-b-deploy-key").mkdir(parents=True, mode=0o700)
    config = Config(
        repo_b=TARGET,
        github_token="rest-identity-token",
        repo_b_rollback_transport="deploy_key",
    )
    monkeypatch.setattr(service, "test_repo_b_deploy_key_access", lambda *_a, **_k: PROOF)
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        with pytest.raises(service.RetriggerCycleError, match="initialized"):
            service._deploy_key_rollback_authority_if_configured(store, config, data)
        store.record_synchronization_baseline(
            TARGET, "main", "e" * 64, BASELINE, synchronized_at=NOW
        )
        authority = service._deploy_key_rollback_authority_if_configured(store, config, data)

    assert authority is not None
    assert "rest-identity-token" not in repr(authority)
