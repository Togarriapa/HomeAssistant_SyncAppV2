import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from ha_syncapp import deploy_key_access
from ha_syncapp.deploy_key import ensure_repo_b_deploy_key
from ha_syncapp.deploy_key_access import (
    MAX_LS_REMOTE_BYTES,
    DeployKeyAccessError,
    test_repo_b_deploy_key_access as prove_access,
)
from ha_syncapp.github_repo import RepoIdentity, RepositoryVerificationError

ROOT = Path(__file__).resolve().parents[1]
TARGET = "Owner/Home"
TOKEN = "secret-token-sentinel"
REPOSITORY_ID = 12345


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    protected.chmod(0o700)
    key_directory = protected / "repo-b-deploy-key"
    ensure_repo_b_deploy_key(key_directory)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    work.chmod(0o700)
    return key_directory, ROOT / "syncapp/github_known_hosts", work


def _trusted_identity(monkeypatch: pytest.MonkeyPatch, order: list[str] | None = None) -> None:
    def verify(target: str, token: str, *, expected_id: int | None = None) -> RepoIdentity:
        assert target == TARGET
        assert token == TOKEN
        assert expected_id == REPOSITORY_ID
        if order is not None:
            order.append("identity")
        return RepoIdentity(target=TARGET, repository_id=REPOSITORY_ID)

    monkeypatch.setattr(deploy_key_access, "fetch_and_verify_private_repository", verify)


def _prove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    output: bytes = b"a" * 40 + b"\trefs/heads/main\n",
):
    key_directory, known_hosts, work = _inputs(tmp_path)
    _trusted_identity(monkeypatch)
    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", lambda *args, **kwargs: output)
    return prove_access(
        TARGET,
        TOKEN,
        REPOSITORY_ID,
        key_directory,
        known_hosts_file=known_hosts,
        work_directory=work,
    )


def test_proof_binds_private_repository_key_generation_and_content_free_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = b"a" * 40 + b"\trefs/heads/main\n" + b"b" * 40 + b"\trefs/tags/v1.0.0\n"
    proof = _prove(tmp_path, monkeypatch, output=output)

    assert proof.target == TARGET
    assert proof.repository_id == REPOSITORY_ID
    assert proof.key_fingerprint.startswith("SHA256:")
    assert proof.generation_id
    assert proof.ref_count == 2
    assert proof.observation_sha256 == hashlib.sha256(output).hexdigest()
    assert "refs/heads/main" not in repr(proof)
    assert "refs/tags/v1.0.0" not in repr(proof)
    assert TOKEN not in repr(proof)


def test_empty_repository_is_valid_authenticated_read_only_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = _prove(tmp_path, monkeypatch, output=b"")
    assert proof.ref_count == 0
    assert proof.observation_sha256 == hashlib.sha256(b"").hexdigest()


def test_identity_is_reproved_before_key_inspection_or_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, work = _inputs(tmp_path)
    order: list[str] = []
    _trusted_identity(monkeypatch, order)
    real_inspect = deploy_key_access.inspect_repo_b_deploy_key

    def inspect(path: Path):
        order.append("inspect")
        return real_inspect(path)

    def run(*args: object, **kwargs: object) -> bytes:
        order.append("ssh")
        return b""

    monkeypatch.setattr(deploy_key_access, "inspect_repo_b_deploy_key", inspect)
    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", run)
    prove_access(
        TARGET,
        TOKEN,
        REPOSITORY_ID,
        key_directory,
        known_hosts_file=known_hosts,
        work_directory=work,
    )
    assert order == ["identity", "inspect", "ssh"]


def test_repository_identity_failure_prevents_key_or_ssh_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)

    def reject(*args: object, **kwargs: object) -> RepoIdentity:
        raise RepositoryVerificationError("secret identity diagnostic")

    monkeypatch.setattr(deploy_key_access, "fetch_and_verify_private_repository", reject)
    monkeypatch.setattr(deploy_key_access, "inspect_repo_b_deploy_key", pytest.fail)
    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", pytest.fail)
    with pytest.raises(DeployKeyAccessError, match="repository identity verification failed") as e:
        prove_access(
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            protected / "repo-b-deploy-key",
            known_hosts_file=ROOT / "syncapp/github_known_hosts",
            work_directory=work,
        )
    assert e.value.transient is False
    assert "secret identity diagnostic" not in str(e.value)


def test_transient_repository_identity_failure_remains_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)

    def reject(*args: object, **kwargs: object) -> RepoIdentity:
        raise RepositoryVerificationError("network sentinel", transient=True)

    monkeypatch.setattr(deploy_key_access, "fetch_and_verify_private_repository", reject)
    with pytest.raises(DeployKeyAccessError) as e:
        prove_access(
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            protected / "repo-b-deploy-key",
            known_hosts_file=ROOT / "syncapp/github_known_hosts",
            work_directory=work,
        )
    assert e.value.transient is True
    assert "network sentinel" not in str(e.value)


def test_command_is_noninteractive_read_only_and_uses_inherited_key_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, work = _inputs(tmp_path)
    private_sentinel = (key_directory / "private_key").read_text()
    _trusted_identity(monkeypatch)
    calls: list[tuple[tuple[str, ...], Path, dict[str, str], tuple[int, ...]]] = []

    def capture(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        pass_fds: tuple[int, ...],
    ) -> bytes:
        calls.append((command, cwd, environment, pass_fds))
        assert len(pass_fds) == 1
        assert stat.S_ISREG(os.fstat(pass_fds[0]).st_mode)
        return b""

    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", capture)
    prove_access(
        TARGET,
        TOKEN,
        REPOSITORY_ID,
        key_directory,
        known_hosts_file=known_hosts,
        work_directory=work,
    )

    assert len(calls) == 1
    command, cwd, environment, pass_fds = calls[0]
    assert command[0] == "/usr/bin/git"
    assert command[-3:] == (
        "ls-remote",
        "--refs",
        "ssh://git@github.com/Owner/Home.git",
    )
    ssh_config = next(value for value in command if value.startswith("core.sshCommand="))
    for required in (
        "/usr/bin/ssh",
        "-F /dev/null",
        f"-i /proc/self/fd/{pass_fds[0]}",
        "IdentitiesOnly=yes",
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={known_hosts}",
        "GlobalKnownHostsFile=/dev/null",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "NumberOfPasswordPrompts=0",
        "ClearAllForwardings=yes",
        "PermitLocalCommand=no",
        "ConnectTimeout=10",
    ):
        assert required in ssh_config
    assert cwd == work
    assert environment == {
        "PATH": "/usr/bin",
        "HOME": str(work),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_SSH_VARIANT": "ssh",
        "LC_ALL": "C",
    }
    encoded = json.dumps([command, environment], sort_keys=True)
    assert TOKEN not in encoded
    assert private_sentinel not in encoded
    assert str(key_directory / "private_key") not in encoded


def test_missing_or_interrupted_key_never_opens_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    _trusted_identity(monkeypatch)
    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", pytest.fail)
    with pytest.raises(DeployKeyAccessError, match="protected deploy key is invalid"):
        prove_access(
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            protected / "repo-b-deploy-key",
            known_hosts_file=ROOT / "syncapp/github_known_hosts",
            work_directory=work,
        )


def test_packaged_github_host_pin_is_exact_and_official() -> None:
    known_hosts = (ROOT / "syncapp/github_known_hosts").read_text()
    assert known_hosts == (
        "github.com ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
    )
    assert deploy_key_access.GITHUB_ED25519_FINGERPRINT == (
        "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
    )


@pytest.mark.parametrize("tamper", ["content", "mode", "symlink", "hardlink"])
def test_tampered_host_pin_fails_before_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    key_directory, packaged, work = _inputs(tmp_path)
    host = tmp_path / "known_hosts"
    host.write_bytes(packaged.read_bytes())
    host.chmod(0o644)
    if tamper == "content":
        host.write_text("github.com ssh-ed25519 AAAAsecret-sentinel\n")
    elif tamper == "mode":
        host.chmod(0o666)
    elif tamper == "symlink":
        host.unlink()
        host.symlink_to(packaged)
    else:
        os.link(host, tmp_path / "known_hosts.alias")
    _trusted_identity(monkeypatch)
    monkeypatch.setattr(deploy_key_access, "_run_git_ls_remote", pytest.fail)

    with pytest.raises(DeployKeyAccessError, match="GitHub host key pin is invalid"):
        prove_access(
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            key_directory,
            known_hosts_file=host,
            work_directory=work,
        )


@pytest.mark.parametrize(
    "output",
    [
        b"not-a-sha\trefs/heads/main\n",
        b"A" * 40 + b"\trefs/heads/main\n",
        b"a" * 40 + b" refs/heads/main\n",
        b"a" * 40 + b"\tHEAD\n",
        b"a" * 40 + b"\trefs/tags/v1^{}\n",
        b"b" * 40 + b"\trefs/tags/v1\n" + b"a" * 40 + b"\trefs/heads/main\n",
        b"a" * 40 + b"\trefs/heads/main\n" + b"a" * 40 + b"\trefs/heads/main\n",
        b"a" * 40 + b"\trefs/heads/main",
    ],
)
def test_malformed_noncanonical_or_duplicate_output_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: bytes
) -> None:
    with pytest.raises(DeployKeyAccessError, match="invalid reference evidence") as e:
        _prove(tmp_path, monkeypatch, output=output)
    assert e.value.transient is False


def test_oversized_output_is_rejected_without_content_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = b"secret-ref-sentinel" + b"x" * MAX_LS_REMOTE_BYTES
    with pytest.raises(DeployKeyAccessError) as e:
        _prove(tmp_path, monkeypatch, output=output)
    assert e.value.transient is False
    assert "secret-ref-sentinel" not in str(e.value)


@pytest.mark.parametrize(
    ("stderr", "transient", "message"),
    [
        ("Permission denied (publickey).", False, "authentication failed"),
        ("ERROR: Repository not found.", False, "authentication failed"),
        ("Host key verification failed.", False, "authentication failed"),
        ("ssh: Could not resolve hostname github.com", True, "transport failed"),
        ("ssh: connect to host github.com port 22: Network is unreachable", True, "transport failed"),
        ("secret-unknown-failure", False, "command failed"),
    ],
)
def test_command_failure_is_sanitized_and_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    transient: bool,
    message: str,
) -> None:
    key_directory, known_hosts, work = _inputs(tmp_path)
    _trusted_identity(monkeypatch)
    executable = tmp_path / "fake-git"
    executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{stderr}' >&2\nexit 128\n")
    executable.chmod(0o700)

    with pytest.raises(DeployKeyAccessError, match=message) as e:
        prove_access(
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            key_directory,
            known_hosts_file=known_hosts,
            work_directory=work,
            git_executable=executable,
        )
    assert e.value.transient is transient
    assert "secret-unknown-failure" not in str(e.value)


def test_command_timeout_is_transient_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, work = _inputs(tmp_path)
    _trusted_identity(monkeypatch)
    executable = tmp_path / "slow-git"
    executable.write_text("#!/bin/sh\nsleep 2\n")
    executable.chmod(0o700)
    monkeypatch.setattr(deploy_key_access, "COMMAND_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(DeployKeyAccessError, match="transport timed out") as e:
        prove_access(
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            key_directory,
            known_hosts_file=known_hosts,
            work_directory=work,
            git_executable=executable,
        )
    assert e.value.transient is True

