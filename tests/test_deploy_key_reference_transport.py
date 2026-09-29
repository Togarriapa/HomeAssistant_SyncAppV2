import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from ha_syncapp import deploy_key_access
from ha_syncapp.deploy_key import ensure_repo_b_deploy_key, inspect_repo_b_deploy_key
from ha_syncapp.deploy_key_access import (
    MAX_LS_REMOTE_BYTES,
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    read_repo_b_deploy_key_references,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = "Owner/Home"
REPOSITORY_ID = 12345


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, DeployKeyAccessProof]:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    protected.chmod(0o700)
    key_directory = protected / "repo-b-deploy-key"
    enrollment = ensure_repo_b_deploy_key(key_directory)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    work.chmod(0o700)
    proof = DeployKeyAccessProof(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        key_fingerprint=enrollment.fingerprint,
        generation_id=enrollment.generation_id,
        ref_count=0,
        observation_sha256=hashlib.sha256(b"").hexdigest(),
    )
    return key_directory, ROOT / "syncapp/github_known_hosts", work, proof


def _read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
):
    key_directory, known_hosts, work, proof = _inputs(tmp_path)
    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", lambda *args, **kwargs: output)
    return read_repo_b_deploy_key_references(
        proof,
        key_directory,
        known_hosts_file=known_hosts,
        work_directory=work,
    )


def test_returns_canonical_identity_and_generation_bound_reference_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = b"a" * 40 + b"\trefs/heads/main\n" + b"b" * 40 + b"\trefs/tags/v1.0.0\n"

    snapshot = _read(tmp_path, monkeypatch, output)

    assert snapshot.target == TARGET
    assert snapshot.repository_id == REPOSITORY_ID
    assert snapshot.key_fingerprint.startswith("SHA256:")
    assert snapshot.generation_id
    assert snapshot.references == (
        DeployKeyReference("refs/heads/main", "a" * 40),
        DeployKeyReference("refs/tags/v1.0.0", "b" * 40),
    )
    assert snapshot.observation_sha256 == hashlib.sha256(output).hexdigest()


def test_empty_repository_has_canonical_empty_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _read(tmp_path, monkeypatch, b"")
    assert snapshot.references == ()
    assert snapshot.observation_sha256 == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(
    "output",
    [
        b"a" * 40 + b"\trefs/heads/main",
        b"a" * 40 + b" refs/heads/main\n",
        b"a" * 40 + b"\tHEAD\n",
        b"a" * 40 + b"\trefs/pull/1/head\n",
        b"a" * 40 + b"\trefs/tags/v1^{}\n",
        b"a" * 40 + b"\trefs/heads/main\n" + b"b" * 40 + b"\trefs/heads/main\n",
        b"b" * 40 + b"\trefs/tags/v1\n" + b"a" * 40 + b"\trefs/heads/main\n",
        b"g" * 40 + b"\trefs/heads/main\n",
    ],
)
def test_malformed_duplicate_or_noncanonical_references_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: bytes
) -> None:
    with pytest.raises(DeployKeyAccessError, match="invalid reference evidence") as error:
        _read(tmp_path, monkeypatch, output)
    assert error.value.transient is False


def test_replaced_generation_is_rejected_before_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, work, proof = _inputs(tmp_path)
    different = tmp_path / "protected" / "replacement"
    replacement = ensure_repo_b_deploy_key(different)
    (key_directory / "private_key").write_bytes((different / "private_key").read_bytes())
    (key_directory / "public_key").write_bytes((different / "public_key").read_bytes())
    (key_directory / "manifest.json").write_bytes((different / "manifest.json").read_bytes())
    assert replacement.generation_id != proof.generation_id
    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", pytest.fail)

    with pytest.raises(DeployKeyAccessError, match="proof does not match"):
        read_repo_b_deploy_key_references(
            proof,
            key_directory,
            known_hosts_file=known_hosts,
            work_directory=work,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"target": "Other/Repo"},
        {"repository_id": 0},
        {"key_fingerprint": "bad"},
        {"generation_id": "bad"},
        {"ref_count": -1},
        {"observation_sha256": "bad"},
    ],
)
def test_forged_or_malformed_proof_is_rejected_before_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    key_directory, known_hosts, work, proof = _inputs(tmp_path)
    values: dict[str, object] = {
        "target": proof.target,
        "repository_id": proof.repository_id,
        "key_fingerprint": proof.key_fingerprint,
        "generation_id": proof.generation_id,
        "ref_count": proof.ref_count,
        "observation_sha256": proof.observation_sha256,
    }
    values.update(change)
    forged = DeployKeyAccessProof(**values)  # type: ignore[arg-type]
    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", pytest.fail)

    with pytest.raises(DeployKeyAccessError, match="proof"):
        read_repo_b_deploy_key_references(
            forged,
            key_directory,
            known_hosts_file=known_hosts,
            work_directory=work,
        )


def test_transport_reuses_confined_ssh_descriptor_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, work, proof = _inputs(tmp_path)
    private_sentinel = (key_directory / "private_key").read_text()
    captured: list[tuple[tuple[str, ...], dict[str, str], tuple[int, ...]]] = []

    def capture(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        pass_fds: tuple[int, ...],
    ) -> bytes:
        assert cwd == work
        assert len(pass_fds) == 1
        assert stat.S_ISREG(os.fstat(pass_fds[0]).st_mode)
        captured.append((command, environment, pass_fds))
        return b""

    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", capture)
    read_repo_b_deploy_key_references(
        proof,
        key_directory,
        known_hosts_file=known_hosts,
        work_directory=work,
    )

    command, environment, descriptors = captured[0]
    assert command[-3:] == ("ls-remote", "--refs", "ssh://git@github.com/Owner/Home.git")
    ssh_config = next(value for value in command if value.startswith("core.sshCommand="))
    assert f"-i /proc/self/fd/{descriptors[0]}" in ssh_config
    assert "StrictHostKeyChecking=yes" in ssh_config
    assert f"UserKnownHostsFile={known_hosts}" in ssh_config
    assert "IdentitiesOnly=yes" in ssh_config
    assert "ClearAllForwardings=yes" in ssh_config
    encoded = json.dumps([command, environment], sort_keys=True)
    assert private_sentinel not in encoded
    assert str(key_directory / "private_key") not in encoded
    assert "credential.helper=" in command
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_oversized_reference_output_is_deterministic_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = "PRIVATE-SENTINEL"
    with pytest.raises(DeployKeyAccessError) as error:
        _read(tmp_path, monkeypatch, (b"x" * MAX_LS_REMOTE_BYTES) + private.encode())
    assert error.value.transient is False
    assert private not in str(error.value)


def test_transport_preserves_transient_failure_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, work, proof = _inputs(tmp_path)

    def unavailable(*args: object, **kwargs: object) -> bytes:
        raise DeployKeyAccessError("Deploy key access transport timed out", transient=True)

    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", unavailable)
    with pytest.raises(DeployKeyAccessError) as error:
        read_repo_b_deploy_key_references(
            proof,
            key_directory,
            known_hosts_file=known_hosts,
            work_directory=work,
        )
    assert error.value.transient is True


def test_snapshot_uses_current_inspected_enrollment_not_mutable_proof_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, _, _, proof = _inputs(tmp_path)
    enrollment = inspect_repo_b_deploy_key(key_directory)
    assert enrollment.generation_id == proof.generation_id
    assert enrollment.fingerprint == proof.key_fingerprint
