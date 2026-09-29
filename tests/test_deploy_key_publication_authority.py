from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp import deploy_key_publication_authority as authority_module
from ha_syncapp.config import Config
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from ha_syncapp.git_workspace import GitWorkspace
from ha_syncapp.github_repo import BranchAbsence, BranchHead
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.state import StateStore, SynchronizationBaseline

TARGET = "Owner/Home"
REPOSITORY_ID = 123
SHA = "a" * 40
NOW = datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
PROOF = DeployKeyAccessProof(
    target=TARGET,
    repository_id=REPOSITORY_ID,
    key_fingerprint="SHA256:" + "A" * 43,
    generation_id="123e4567-e89b-42d3-a456-426614174000",
    ref_count=2,
    observation_sha256="b" * 64,
)


def _authority(tmp_path: Path) -> authority_module.DeployKeyPublicationAuthority:
    key = tmp_path / "key"
    access = tmp_path / "access"
    key.mkdir(mode=0o700)
    access.mkdir(mode=0o700)
    return authority_module.DeployKeyPublicationAuthority(PROOF, key, access)


def test_observe_returns_exact_branch_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[object, ...]] = []

    def read(*args: object, **kwargs: object) -> DeployKeyReferenceSnapshot:
        observed.append((*args, kwargs))
        return DeployKeyReferenceSnapshot(
            TARGET,
            REPOSITORY_ID,
            PROOF.key_fingerprint,
            PROOF.generation_id,
            (
                DeployKeyReference("refs/heads/database", "c" * 40),
                DeployKeyReference("refs/heads/main", SHA),
            ),
            "d" * 64,
        )

    monkeypatch.setattr(authority_module, "read_repo_b_deploy_key_references", read)
    authority = _authority(tmp_path)

    assert authority.observe(TARGET, REPOSITORY_ID, "main") == BranchHead(
        TARGET, REPOSITORY_ID, "main", SHA
    )
    assert authority.observe(TARGET, REPOSITORY_ID, "logs") == BranchAbsence(
        TARGET, REPOSITORY_ID, "logs"
    )
    assert observed[0][:4] == (PROOF, TARGET, REPOSITORY_ID, tmp_path / "key")
    assert "token" not in repr(observed)


def test_observe_rejects_duplicate_or_rebound_branch_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _authority(tmp_path)
    for snapshot in (
        DeployKeyReferenceSnapshot(
            TARGET,
            REPOSITORY_ID,
            PROOF.key_fingerprint,
            PROOF.generation_id,
            (
                DeployKeyReference("refs/heads/main", SHA),
                DeployKeyReference("refs/heads/main", "c" * 40),
            ),
            "d" * 64,
        ),
        DeployKeyReferenceSnapshot(
            "Other/Repo",
            REPOSITORY_ID,
            PROOF.key_fingerprint,
            PROOF.generation_id,
            (DeployKeyReference("refs/heads/main", SHA),),
            "d" * 64,
        ),
    ):
        monkeypatch.setattr(
            authority_module,
            "read_repo_b_deploy_key_references",
            lambda *args, snapshot=snapshot, **kwargs: snapshot,
        )
        with pytest.raises(
            authority_module.DeployKeyPublicationAuthorityError,
            match="invalid",
        ):
            authority.observe(TARGET, REPOSITORY_ID, "main")


@pytest.mark.parametrize("transient", [False, True])
def test_observe_preserves_sanitized_retry_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transient: bool,
) -> None:
    authority = _authority(tmp_path)
    monkeypatch.setattr(
        authority_module,
        "read_repo_b_deploy_key_references",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            DeployKeyAccessError("secret-sensitive-detail", transient=transient)
        ),
    )

    with pytest.raises(authority_module.DeployKeyPublicationAuthorityError) as caught:
        authority.observe(TARGET, REPOSITORY_ID, "main")

    assert caught.value.transient is transient
    assert "secret-sensitive-detail" not in str(caught.value)


def test_anchor_uses_only_proof_bound_deploy_key_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _authority(tmp_path)
    workspace = GitWorkspace("e" * 64, tmp_path / "workspace", tmp_path / "workspace/tree")
    remote = BranchHead(TARGET, REPOSITORY_ID, "main", SHA)
    observed: list[tuple[object, ...]] = []

    def anchor(*args: object, **kwargs: object) -> str:
        observed.append((*args, kwargs))
        return SHA

    monkeypatch.setattr(authority_module, "anchor_trusted_baseline_with_deploy_key", anchor)

    assert authority.anchor(workspace, remote) == SHA
    assert observed[0][:4] == (workspace, remote, PROOF, tmp_path / "key")
    assert "token" not in repr(observed)


def test_complete_confirms_exact_result_before_persisting_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _authority(tmp_path)
    workspace = GitWorkspace("e" * 64, tmp_path / "workspace", tmp_path / "workspace/tree")
    intent = PublicationIntent(TARGET, REPOSITORY_ID, "main", "c" * 40, SHA, False)
    baseline = SynchronizationBaseline(TARGET, "main", "e" * 64, "c" * 40, NOW)
    observations = iter((SHA, "c" * 40))
    pushes: list[tuple[object, ...]] = []
    persisted: list[tuple[object, ...]] = []

    def read(*args: object, **kwargs: object) -> DeployKeyReferenceSnapshot:
        return DeployKeyReferenceSnapshot(
            TARGET,
            REPOSITORY_ID,
            PROOF.key_fingerprint,
            PROOF.generation_id,
            (DeployKeyReference("refs/heads/main", next(observations)),),
            "d" * 64,
        )

    def push(*args: object, **kwargs: object) -> str:
        pushes.append((*args, kwargs))
        return "c" * 40

    def record(*args: object, **kwargs: object) -> SynchronizationBaseline:
        persisted.append((*args, kwargs))
        return baseline

    monkeypatch.setattr(authority_module, "read_repo_b_deploy_key_references", read)
    monkeypatch.setattr(authority_module, "push_publication_intent_with_deploy_key", push)
    monkeypatch.setattr(authority_module, "record_verified_publication", record)

    (tmp_path / "data").mkdir()
    with StateStore(tmp_path / "data") as store:
        assert authority.complete(store, workspace, intent, synchronized_at=NOW) == baseline

    assert pushes[0][:4] == (workspace, intent, PROOF, tmp_path / "key")
    assert persisted[0][:4] == (
        store,
        workspace,
        intent,
        authority_module.verify_publication_result(
            intent, BranchHead(TARGET, REPOSITORY_ID, "main", "c" * 40)
        ),
    )
    assert "token" not in repr(pushes)


def test_authority_hides_protected_paths_and_rejects_mixed_credentials(
    tmp_path: Path,
) -> None:
    authority = _authority(tmp_path)
    rendered = repr(authority)
    assert str(tmp_path / "key") not in rendered
    assert str(tmp_path / "access") not in rendered
    assert authority_module.resolve_publication_authority(authority) is authority
    with pytest.raises(
        authority_module.DeployKeyPublicationAuthorityError,
        match="exactly one",
    ):
        authority_module.resolve_publication_authority(authority, token="secret")
    with pytest.raises(
        authority_module.DeployKeyPublicationAuthorityError,
        match="exactly one",
    ):
        authority_module.resolve_publication_authority(None, token=None)


def test_service_builds_publication_authority_only_after_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    protected = data / "syncapp"
    key = protected / "repo-b-deploy-key"
    key.mkdir(parents=True, mode=0o700)
    config = Config(
        repo_b=TARGET,
        github_token="rest-identity-token",
        repo_b_publication_transport="deploy_key",
    )
    calls: list[tuple[object, ...]] = []

    def prove(*args: object, **kwargs: object) -> DeployKeyAccessProof:
        calls.append((*args, kwargs))
        return PROOF

    monkeypatch.setattr(service, "test_repo_b_deploy_key_access", prove)
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        with pytest.raises(service.LocalStartupError, match="initialized"):
            service._deploy_key_publication_authority_if_configured(store, config, data)
        store.record_synchronization_baseline(
            TARGET, "main", "e" * 64, "c" * 40, synchronized_at=NOW
        )
        authority = service._deploy_key_publication_authority_if_configured(store, config, data)

    assert authority is not None
    assert calls[0][:4] == (
        TARGET,
        "rest-identity-token",
        REPOSITORY_ID,
        key,
    )
    assert "rest-identity-token" not in repr(authority)


def test_service_sanitizes_publication_authority_activation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    (data / "syncapp" / "repo-b-deploy-key").mkdir(parents=True, mode=0o700)
    config = Config(
        repo_b=TARGET,
        github_token="rest-identity-token",
        repo_b_publication_transport="deploy_key",
    )
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        store.record_synchronization_baseline(
            TARGET, "main", "e" * 64, "c" * 40, synchronized_at=NOW
        )
        monkeypatch.setattr(
            service,
            "test_repo_b_deploy_key_access",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                service.DeployKeyAccessError("rest-identity-token private path detail")
            ),
        )
        with pytest.raises(
            service.LocalStartupError,
            match="publication deploy-key authority is unavailable",
        ) as caught:
            service._deploy_key_publication_authority_if_configured(store, config, data)

    assert "rest-identity-token" not in str(caught.value)
