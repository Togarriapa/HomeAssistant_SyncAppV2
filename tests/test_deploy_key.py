import json
import os
import stat
from pathlib import Path
from uuid import UUID

import pytest
from ha_syncapp import deploy_key
from ha_syncapp.deploy_key import (
    DeployKeyError,
    ensure_repo_b_deploy_key,
    inspect_repo_b_deploy_key,
)


def _key_path(tmp_path: Path) -> Path:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    protected.chmod(0o700)
    return protected / "repo-b-deploy-key"


def test_generates_private_key_and_returns_only_enrollment_metadata(tmp_path: Path) -> None:
    key_path = _key_path(tmp_path)

    enrollment = ensure_repo_b_deploy_key(key_path)

    assert enrollment.algorithm == "ssh-ed25519"
    assert enrollment.public_key.startswith("ssh-ed25519 AAAA")
    assert enrollment.public_key.endswith(" homeassistant-syncapp-repo-b")
    assert enrollment.fingerprint.startswith("SHA256:")
    assert UUID(enrollment.generation_id)
    assert "PRIVATE" not in repr(enrollment)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((key_path / "private_key").stat().st_mode) == 0o600
    assert stat.S_IMODE((key_path / "public_key").stat().st_mode) == 0o644
    assert stat.S_IMODE((key_path / "manifest.json").stat().st_mode) == 0o600
    assert {entry.name for entry in key_path.iterdir()} == {
        "private_key",
        "public_key",
        "manifest.json",
    }


def test_replay_does_not_invoke_ssh_keygen_or_change_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = _key_path(tmp_path)
    first = ensure_repo_b_deploy_key(key_path)
    before = {entry.name: entry.read_bytes() for entry in key_path.iterdir()}

    monkeypatch.setattr(
        deploy_key,
        "_run_ssh_keygen",
        lambda *args, **kwargs: pytest.fail("replay invoked ssh-keygen"),
    )
    second = ensure_repo_b_deploy_key(key_path)

    assert second == first
    assert {entry.name: entry.read_bytes() for entry in key_path.iterdir()} == before


def test_read_only_inspection_requires_existing_complete_generation(tmp_path: Path) -> None:
    key_path = _key_path(tmp_path)
    with pytest.raises(DeployKeyError, match="Deploy key state is invalid"):
        inspect_repo_b_deploy_key(key_path)

    enrollment = ensure_repo_b_deploy_key(key_path)
    before = {entry.name: entry.read_bytes() for entry in key_path.iterdir()}
    assert inspect_repo_b_deploy_key(key_path) == enrollment
    assert {entry.name: entry.read_bytes() for entry in key_path.iterdir()} == before


def test_read_only_inspection_never_reconciles_generation_journal(tmp_path: Path) -> None:
    key_path = _key_path(tmp_path)
    enrollment = ensure_repo_b_deploy_key(key_path)
    journal = key_path.parent / ".repo-b-deploy-key.generation.json"
    journal.write_text(
        json.dumps(
            {"schema_version": 1, "generation_id": enrollment.generation_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    journal.chmod(0o600)

    with pytest.raises(DeployKeyError, match="Deploy key state is incomplete"):
        inspect_repo_b_deploy_key(key_path)
    assert journal.is_file()


def test_generation_uses_bounded_ed25519_command_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = _key_path(tmp_path)
    calls: list[tuple[tuple[str, ...], Path]] = []
    real = deploy_key._run_ssh_keygen

    def capture(command: tuple[str, ...], *, cwd: Path) -> bytes:
        journal = key_path.parent / ".repo-b-deploy-key.generation.json"
        assert journal.is_file()
        assert stat.S_IMODE(journal.stat().st_mode) == 0o600
        assert not key_path.exists()
        assert stat.S_IMODE(cwd.stat().st_mode) == 0o700
        calls.append((command, cwd))
        return real(command, cwd=cwd)

    monkeypatch.setattr(deploy_key, "_run_ssh_keygen", capture)
    ensure_repo_b_deploy_key(key_path, ssh_keygen=Path("/usr/bin/ssh-keygen"))

    assert len(calls) == 2
    create, derive = calls
    assert create[0] == (
        "/usr/bin/ssh-keygen",
        "-q",
        "-t",
        "ed25519",
        "-N",
        "",
        "-C",
        "homeassistant-syncapp-repo-b",
        "-f",
        str(create[1] / "private_key"),
    )
    assert derive[0] == (
        "/usr/bin/ssh-keygen",
        "-y",
        "-f",
        str(derive[1] / "private_key"),
    )
    assert create[1] == derive[1]
    assert create[1].parent == key_path.parent
    assert create[1] != key_path


@pytest.mark.parametrize("name", ["private_key", "public_key", "manifest.json"])
def test_symlinked_key_files_are_rejected(tmp_path: Path, name: str) -> None:
    key_path = _key_path(tmp_path)
    ensure_repo_b_deploy_key(key_path)
    target = tmp_path / "outside"
    target.write_bytes(b"preserve-me")
    (key_path / name).unlink()
    (key_path / name).symlink_to(target)

    with pytest.raises(DeployKeyError, match="Deploy key state is invalid"):
        ensure_repo_b_deploy_key(key_path)
    assert target.read_bytes() == b"preserve-me"


def test_symlinked_key_directory_is_rejected(tmp_path: Path) -> None:
    key_path = _key_path(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    key_path.symlink_to(outside, target_is_directory=True)

    with pytest.raises(DeployKeyError, match="Deploy key state is invalid"):
        ensure_repo_b_deploy_key(key_path)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("name", ["private_key", "public_key", "manifest.json"])
def test_hardlinked_key_files_are_rejected(tmp_path: Path, name: str) -> None:
    key_path = _key_path(tmp_path)
    ensure_repo_b_deploy_key(key_path)
    original = key_path / name
    alias = tmp_path / f"{name}.alias"
    os.link(original, alias)

    with pytest.raises(DeployKeyError, match="Deploy key state is invalid"):
        ensure_repo_b_deploy_key(key_path)


def test_special_file_and_extra_entry_are_rejected(tmp_path: Path) -> None:
    key_path = _key_path(tmp_path)
    ensure_repo_b_deploy_key(key_path)
    (key_path / "unexpected").write_text("reject")

    with pytest.raises(DeployKeyError, match="Deploy key state is invalid"):
        ensure_repo_b_deploy_key(key_path)

    (key_path / "unexpected").unlink()
    (key_path / "public_key").unlink()
    os.mkfifo(key_path / "public_key", mode=0o644)
    with pytest.raises(DeployKeyError, match="Deploy key state is invalid"):
        ensure_repo_b_deploy_key(key_path)


@pytest.mark.parametrize(
    ("target", "mode"),
    [("directory", 0o755), ("private_key", 0o644), ("public_key", 0o600), ("manifest.json", 0o644)],
)
def test_unsafe_permissions_are_rejected(tmp_path: Path, target: str, mode: int) -> None:
    key_path = _key_path(tmp_path)
    ensure_repo_b_deploy_key(key_path)
    path = key_path if target == "directory" else key_path / target
    path.chmod(mode)

    with pytest.raises(DeployKeyError, match="Deploy key state is invalid"):
        ensure_repo_b_deploy_key(key_path)


def test_public_private_mismatch_is_detected_during_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = _key_path(tmp_path)
    real = deploy_key._run_ssh_keygen

    def mismatch(command: tuple[str, ...], *, cwd: Path) -> bytes:
        result = real(command, cwd=cwd)
        if "-y" in command:
            return (
                b"ssh-ed25519 "
                b"AAAAC3NzaC1lZDI1NTE5AAAAIGZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZm\n"
            )
        return result

    monkeypatch.setattr(deploy_key, "_run_ssh_keygen", mismatch)
    with pytest.raises(DeployKeyError, match="Deploy key generation failed"):
        ensure_repo_b_deploy_key(key_path)
    assert not key_path.exists()


@pytest.mark.parametrize("name", ["private_key", "public_key", "manifest.json"])
def test_tampering_is_detected_without_disclosing_content(tmp_path: Path, name: str) -> None:
    key_path = _key_path(tmp_path)
    ensure_repo_b_deploy_key(key_path)
    sentinel = b"secret-private-sentinel"
    path = key_path / name
    path.write_bytes(sentinel)
    path.chmod(0o644 if name == "public_key" else 0o600)

    with pytest.raises(DeployKeyError) as error:
        ensure_repo_b_deploy_key(key_path)
    assert "secret-private-sentinel" not in str(error.value)
    assert "secret-private-sentinel" not in repr(error.value)


def test_oversized_public_material_is_rejected_before_parsing(tmp_path: Path) -> None:
    key_path = _key_path(tmp_path)
    ensure_repo_b_deploy_key(key_path)
    public = key_path / "public_key"
    public.write_bytes(b"x" * (deploy_key.MAX_PUBLIC_KEY_BYTES + 1))
    public.chmod(0o644)

    with pytest.raises(DeployKeyError, match="Deploy key state is invalid"):
        ensure_repo_b_deploy_key(key_path)


def test_subprocess_failure_leaves_journal_and_never_publishes_partial_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = _key_path(tmp_path)

    def fail(*args: object, **kwargs: object) -> bytes:
        raise DeployKeyError("secret-subprocess-output")

    monkeypatch.setattr(deploy_key, "_run_ssh_keygen", fail)
    with pytest.raises(DeployKeyError, match="Deploy key generation failed") as error:
        ensure_repo_b_deploy_key(key_path)
    assert "secret-subprocess-output" not in str(error.value)
    assert not key_path.exists()
    journals = list(key_path.parent.glob(".repo-b-deploy-key.generation.json"))
    assert len(journals) == 1
    assert stat.S_IMODE(journals[0].stat().st_mode) == 0o600

    monkeypatch.setattr(deploy_key, "_run_ssh_keygen", pytest.fail)
    with pytest.raises(DeployKeyError, match="Deploy key state is incomplete"):
        ensure_repo_b_deploy_key(key_path)


def test_completed_publish_with_journal_is_reconciled_without_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = _key_path(tmp_path)
    enrollment = ensure_repo_b_deploy_key(key_path)
    journal = key_path.parent / ".repo-b-deploy-key.generation.json"
    journal.write_text(
        json.dumps(
            {"schema_version": 1, "generation_id": enrollment.generation_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    journal.chmod(0o600)
    monkeypatch.setattr(
        deploy_key,
        "_run_ssh_keygen",
        lambda *args, **kwargs: pytest.fail("reconciliation regenerated the key"),
    )

    assert ensure_repo_b_deploy_key(key_path) == enrollment
    assert not journal.exists()


def test_unsafe_parent_is_rejected_without_writing(tmp_path: Path) -> None:
    key_path = _key_path(tmp_path)
    key_path.parent.chmod(0o755)

    with pytest.raises(DeployKeyError, match="Deploy key parent is invalid"):
        ensure_repo_b_deploy_key(key_path)
    assert not key_path.exists()


def test_relative_or_non_normalized_target_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DeployKeyError, match="Deploy key parent is invalid"):
        ensure_repo_b_deploy_key(Path("relative/repo-b-deploy-key"))
    with pytest.raises(DeployKeyError, match="Deploy key parent is invalid"):
        ensure_repo_b_deploy_key(tmp_path / "protected" / ".." / "repo-b-deploy-key")


def test_generator_symlink_output_is_rejected_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = _key_path(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve-me")
    outside.chmod(0o644)

    def forged(command: tuple[str, ...], *, cwd: Path) -> bytes:
        if "-t" in command:
            (cwd / "private_key").symlink_to(outside)
            (cwd / "private_key.pub").write_text(
                "ssh-ed25519 "
                "AAAAC3NzaC1lZDI1NTE5AAAAIGZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZm "
                "homeassistant-syncapp-repo-b\n"
            )
            return b""
        return b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZm\n"

    monkeypatch.setattr(deploy_key, "_run_ssh_keygen", forged)
    with pytest.raises(DeployKeyError, match="Deploy key generation failed"):
        ensure_repo_b_deploy_key(key_path)
    assert outside.read_bytes() == b"preserve-me"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
