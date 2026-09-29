import json
from pathlib import Path

import pytest
from ha_syncapp import deploy_key_rollback_authority as rollback_authority
from ha_syncapp.deploy_key_access import DeployKeyAccessProof, DeployKeyReference, DeployKeyReferenceSnapshot
from ha_syncapp.config import load_config


def test_rollback_transport_is_explicit_and_defaults_to_token(tmp_path) -> None:
    default_path = tmp_path / "default.json"
    default_path.write_text("{}")
    assert load_config(default_path).repo_b_rollback_transport == "token"

    selected_path = tmp_path / "selected.json"
    selected_path.write_text(
        json.dumps(
            {
                "repo_b": "Owner/Home",
                "github_token": "test-token",
                "repo_b_rollback_transport": "deploy_key",
            }
        )
    )
    assert load_config(selected_path).repo_b_rollback_transport == "deploy_key"


def test_rollback_transport_rejects_unknown_value(tmp_path) -> None:
    from ha_syncapp.config import ConfigError

    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            {
                "repo_b": "Owner/Home",
                "github_token": "test-token",
                "repo_b_rollback_transport": "other",
            }
        )
    )
    try:
        load_config(path)
    except ConfigError:
        return
    raise AssertionError("invalid rollback transport was accepted")


def test_deploy_key_rollback_transport_requires_repo_b(tmp_path) -> None:
    from ha_syncapp.config import ConfigError

    path = tmp_path / "missing-repo.json"
    path.write_text(json.dumps({"repo_b_rollback_transport": "deploy_key"}))
    with pytest.raises(ConfigError):
        load_config(path)


def test_deploy_key_rollback_authority_reads_exact_main(tmp_path: Path, monkeypatch) -> None:
    proof = DeployKeyAccessProof(
        target="Owner/Home",
        repository_id=123,
        key_fingerprint="SHA256:" + "A" * 43,
        generation_id="123e4567-e89b-42d3-a456-426614174000",
        ref_count=1,
        observation_sha256="b" * 64,
    )
    key = tmp_path / "key"
    access = tmp_path / "access"
    key.mkdir(mode=0o700)
    access.mkdir(mode=0o700)
    snapshot = DeployKeyReferenceSnapshot(
        "Owner/Home",
        123,
        proof.key_fingerprint,
        proof.generation_id,
        (DeployKeyReference("refs/heads/main", "a" * 40),),
        "c" * 64,
    )
    monkeypatch.setattr(
        rollback_authority,
        "read_repo_b_deploy_key_references",
        lambda *_args, **_kwargs: snapshot,
    )
    result = rollback_authority.DeployKeyRollbackRepositoryAuthority(
        proof, key, access
    ).read("Owner/Home", 123)
    assert result.repository_id == 123
    assert result.private is True
    assert result.main_sha == "a" * 40
