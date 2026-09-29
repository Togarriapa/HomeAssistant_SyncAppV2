from __future__ import annotations

from pathlib import Path

import pytest
from ha_syncapp import deploy_key_publication_authority as authority_module
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from ha_syncapp.github_repo import BranchAbsence, BranchHead


TARGET = "Owner/Home"
REPOSITORY_ID = 123
SHA = "a" * 40
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
