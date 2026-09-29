import json
import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from ha_syncapp import deploy_key_rotation
from ha_syncapp.deploy_key import ensure_repo_b_deploy_key, inspect_repo_b_deploy_key
from ha_syncapp.deploy_key_access import DeployKeyAccessError, DeployKeyAccessProof
from ha_syncapp.deploy_key_rotation import (
    DeployKeyRotationError,
    activate_repo_b_deploy_key_rotation,
    inspect_repo_b_deploy_key_rotation,
    prepare_repo_b_deploy_key_rotation,
    verify_repo_b_deploy_key_rotation,
)

TARGET = "Owner/Home"
REPOSITORY_ID = 12345
TOKEN = "secret-token-sentinel"


def _active(tmp_path: Path) -> tuple[Path, object]:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    protected.chmod(0o700)
    active = protected / "repo-b-deploy-key"
    return active, ensure_repo_b_deploy_key(active)


def _prepare(tmp_path: Path) -> tuple[Path, str, object, object]:
    active, old = _active(tmp_path)
    request_id = str(uuid4())
    status = prepare_repo_b_deploy_key_rotation(
        active,
        request_id,
        TARGET,
        REPOSITORY_ID,
    )
    return active, request_id, old, status


def _proof(status: object) -> DeployKeyAccessProof:
    return DeployKeyAccessProof(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        key_fingerprint=status.candidate_fingerprint,
        generation_id=status.candidate_generation_id,
        ref_count=2,
        observation_sha256="a" * 64,
    )


def _verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, object, object]:
    active, request_id, old, prepared = _prepare(tmp_path)
    monkeypatch.setattr(
        deploy_key_rotation,
        "test_repo_b_deploy_key_access",
        lambda *args, **kwargs: _proof(prepared),
    )
    status = verify_repo_b_deploy_key_rotation(
        active,
        request_id,
        TARGET,
        TOKEN,
        REPOSITORY_ID,
        work_directory=active.parent,
    )
    return active, request_id, old, status


def test_prepare_journals_before_generation_and_returns_only_enrollment_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, old = _active(tmp_path)
    request_id = str(uuid4())
    real = deploy_key_rotation.ensure_repo_b_deploy_key
    observed: dict[str, object] = {}

    def capture(path: Path, *, ssh_keygen: Path) -> object:
        record_path = active.parent / ".repo-b-deploy-key.rotation.json"
        observed["record"] = json.loads(record_path.read_text())
        observed["mode"] = stat.S_IMODE(record_path.stat().st_mode)
        observed["path"] = path
        assert inspect_repo_b_deploy_key(active) == old
        return real(path, ssh_keygen=ssh_keygen)

    monkeypatch.setattr(deploy_key_rotation, "ensure_repo_b_deploy_key", capture)
    status = prepare_repo_b_deploy_key_rotation(
        active,
        request_id,
        TARGET,
        REPOSITORY_ID,
    )

    assert observed["record"]["phase"] == "preparing"
    assert observed["mode"] == 0o600
    assert observed["path"] == active.parent / ".repo-b-deploy-key.rotation-candidate"
    assert status.request_id == request_id
    assert UUID(status.rotation_id)
    assert status.phase == "prepared"
    assert status.target == TARGET
    assert status.repository_id == REPOSITORY_ID
    assert status.active_generation_id == old.generation_id
    assert status.active_fingerprint == old.fingerprint
    assert status.candidate_generation_id != old.generation_id
    assert status.candidate_fingerprint != old.fingerprint
    assert status.candidate_public_key.startswith("ssh-ed25519 AAAA")
    assert status.observation_sha256 is None
    assert status.retained_generation_id is None
    assert "private" not in repr(status).casefold()
    assert str(active) not in repr(status)
    assert TOKEN not in repr(status)


def test_prepare_replay_is_network_free_and_does_not_generate_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, _, first = _prepare(tmp_path)
    monkeypatch.setattr(deploy_key_rotation, "ensure_repo_b_deploy_key", pytest.fail)
    monkeypatch.setattr(deploy_key_rotation, "test_repo_b_deploy_key_access", pytest.fail)

    second = prepare_repo_b_deploy_key_rotation(active, request_id, TARGET, REPOSITORY_ID)

    assert second == first


def test_competing_request_cannot_rebind_rotation(tmp_path: Path) -> None:
    active, _, _, _ = _prepare(tmp_path)
    with pytest.raises(DeployKeyRotationError, match="another request"):
        prepare_repo_b_deploy_key_rotation(active, str(uuid4()), TARGET, REPOSITORY_ID)


@pytest.mark.parametrize(
    ("request_id", "target", "repository_id"),
    [
        ("not-a-uuid", TARGET, REPOSITORY_ID),
        (str(uuid4()).upper(), TARGET, REPOSITORY_ID),
        (str(uuid4()), "Owner/Home/Extra", REPOSITORY_ID),
        (str(uuid4()), TARGET, 0),
        (str(uuid4()), TARGET, True),
    ],
)
def test_invalid_prepare_input_writes_nothing(
    tmp_path: Path, request_id: str, target: str, repository_id: int
) -> None:
    active, old = _active(tmp_path)
    with pytest.raises(DeployKeyRotationError, match="invalid"):
        prepare_repo_b_deploy_key_rotation(active, request_id, target, repository_id)
    assert inspect_repo_b_deploy_key(active) == old
    assert {entry.name for entry in active.parent.iterdir()} == {active.name}


def test_interrupted_generation_blocks_without_changing_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, old = _active(tmp_path)
    request_id = str(uuid4())

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("secret-generator-diagnostic")

    monkeypatch.setattr(deploy_key_rotation, "ensure_repo_b_deploy_key", fail)
    with pytest.raises(DeployKeyRotationError, match="preparation failed") as error:
        prepare_repo_b_deploy_key_rotation(active, request_id, TARGET, REPOSITORY_ID)
    assert "secret-generator-diagnostic" not in str(error.value)
    assert inspect_repo_b_deploy_key(active) == old

    monkeypatch.setattr(deploy_key_rotation, "ensure_repo_b_deploy_key", pytest.fail)
    with pytest.raises(DeployKeyRotationError, match="incomplete"):
        prepare_repo_b_deploy_key_rotation(active, request_id, TARGET, REPOSITORY_ID)


def test_verification_uses_exact_candidate_and_persists_content_free_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, _, prepared = _prepare(tmp_path)
    calls: list[tuple[object, ...]] = []

    def prove(*args: object, **kwargs: object) -> DeployKeyAccessProof:
        calls.append(args + (kwargs,))
        return _proof(prepared)

    monkeypatch.setattr(deploy_key_rotation, "test_repo_b_deploy_key_access", prove)
    status = verify_repo_b_deploy_key_rotation(
        active,
        request_id,
        TARGET,
        TOKEN,
        REPOSITORY_ID,
        known_hosts_file=tmp_path / "known_hosts",
        work_directory=active.parent,
        git_executable=Path("/usr/bin/git"),
        ssh_executable=Path("/usr/bin/ssh"),
    )

    assert status.phase == "verified"
    assert status.observation_sha256 == "a" * 64
    assert len(calls) == 1
    assert calls[0][:4] == (
        TARGET,
        TOKEN,
        REPOSITORY_ID,
        active.parent / ".repo-b-deploy-key.rotation-candidate",
    )
    assert TOKEN not in repr(status)
    record = (active.parent / ".repo-b-deploy-key.rotation.json").read_text()
    assert TOKEN not in record
    assert "refs/" not in record


def test_verified_replay_does_not_repeat_access_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, _, first = _verify(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy_key_rotation, "test_repo_b_deploy_key_access", pytest.fail)

    second = verify_repo_b_deploy_key_rotation(
        active,
        request_id,
        TARGET,
        TOKEN,
        REPOSITORY_ID,
        work_directory=active.parent,
    )
    assert second == first


def test_transient_verification_failure_remains_prepared_for_explicit_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, _, prepared = _prepare(tmp_path)
    attempts = 0

    def prove(*args: object, **kwargs: object) -> DeployKeyAccessProof:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DeployKeyAccessError("secret-network-diagnostic", transient=True)
        return _proof(prepared)

    monkeypatch.setattr(deploy_key_rotation, "test_repo_b_deploy_key_access", prove)
    with pytest.raises(DeployKeyRotationError, match="temporarily unavailable") as error:
        verify_repo_b_deploy_key_rotation(
            active,
            request_id,
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            work_directory=active.parent,
        )
    assert error.value.transient is True
    assert "secret-network-diagnostic" not in str(error.value)
    assert inspect_repo_b_deploy_key_rotation(active, request_id).phase == "prepared"

    assert (
        verify_repo_b_deploy_key_rotation(
            active,
            request_id,
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            work_directory=active.parent,
        ).phase
        == "verified"
    )
    assert attempts == 2


def test_deterministic_verification_failure_is_durably_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, old, _ = _prepare(tmp_path)
    monkeypatch.setattr(
        deploy_key_rotation,
        "test_repo_b_deploy_key_access",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            DeployKeyAccessError("secret-auth-diagnostic")
        ),
    )
    with pytest.raises(DeployKeyRotationError, match="blocked") as error:
        verify_repo_b_deploy_key_rotation(
            active,
            request_id,
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            work_directory=active.parent,
        )
    assert error.value.transient is False
    assert "secret-auth-diagnostic" not in str(error.value)
    assert inspect_repo_b_deploy_key(active) == old
    assert inspect_repo_b_deploy_key_rotation(active, request_id).phase == "blocked"

    monkeypatch.setattr(deploy_key_rotation, "test_repo_b_deploy_key_access", pytest.fail)
    with pytest.raises(DeployKeyRotationError, match="blocked"):
        verify_repo_b_deploy_key_rotation(
            active,
            request_id,
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            work_directory=active.parent,
        )


@pytest.mark.parametrize(
    "field",
    ["target", "repository_id", "key_fingerprint", "generation_id", "observation_sha256"],
)
def test_mismatched_access_proof_blocks_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    active, request_id, _, prepared = _prepare(tmp_path)
    values: dict[str, object] = {
        "target": TARGET,
        "repository_id": REPOSITORY_ID,
        "key_fingerprint": prepared.candidate_fingerprint,
        "generation_id": prepared.candidate_generation_id,
        "ref_count": 1,
        "observation_sha256": "a" * 64,
    }
    values[field] = {
        "target": "Other/Repo",
        "repository_id": 999,
        "key_fingerprint": "SHA256:wrong",
        "generation_id": str(uuid4()),
        "observation_sha256": "A" * 64,
    }[field]
    monkeypatch.setattr(
        deploy_key_rotation,
        "test_repo_b_deploy_key_access",
        lambda *args, **kwargs: DeployKeyAccessProof(**values),
    )
    with pytest.raises(DeployKeyRotationError, match="blocked"):
        verify_repo_b_deploy_key_rotation(
            active,
            request_id,
            TARGET,
            TOKEN,
            REPOSITORY_ID,
            work_directory=active.parent,
        )
    assert inspect_repo_b_deploy_key_rotation(active, request_id).phase == "blocked"


def test_activation_requires_verified_access(tmp_path: Path) -> None:
    active, request_id, old, _ = _prepare(tmp_path)
    with pytest.raises(DeployKeyRotationError, match="not verified"):
        activate_repo_b_deploy_key_rotation(active, request_id)
    assert inspect_repo_b_deploy_key(active) == old


def test_activation_journals_before_swap_and_retains_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, old, verified = _verify(tmp_path, monkeypatch)
    calls: list[tuple[Path, Path, str]] = []
    real = deploy_key_rotation._rename_directory

    def capture(source: Path, destination: Path) -> None:
        phase = json.loads((active.parent / ".repo-b-deploy-key.rotation.json").read_text())[
            "phase"
        ]
        calls.append((source, destination, phase))
        real(source, destination)

    monkeypatch.setattr(deploy_key_rotation, "_rename_directory", capture)
    status = activate_repo_b_deploy_key_rotation(active, request_id)

    retained = active.parent / ".repo-b-deploy-key.rotation-retained"
    assert calls == [
        (active, retained, "activating"),
        (active.parent / ".repo-b-deploy-key.rotation-candidate", active, "activating"),
    ]
    assert status.phase == "activated"
    assert status.active_generation_id == verified.candidate_generation_id
    assert status.active_fingerprint == verified.candidate_fingerprint
    assert status.retained_generation_id == old.generation_id
    assert status.retained_fingerprint == old.fingerprint
    assert inspect_repo_b_deploy_key(active).generation_id == verified.candidate_generation_id
    assert inspect_repo_b_deploy_key(retained) == old


def test_activation_recovers_crash_between_directory_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, old, verified = _verify(tmp_path, monkeypatch)
    real = deploy_key_rotation._rename_directory
    calls = 0

    def interrupt(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash")
        real(source, destination)

    monkeypatch.setattr(deploy_key_rotation, "_rename_directory", interrupt)
    with pytest.raises(DeployKeyRotationError, match="interrupted"):
        activate_repo_b_deploy_key_rotation(active, request_id)
    assert not active.exists()
    assert inspect_repo_b_deploy_key(active.parent / ".repo-b-deploy-key.rotation-retained") == old

    monkeypatch.setattr(deploy_key_rotation, "_rename_directory", real)
    status = activate_repo_b_deploy_key_rotation(active, request_id)
    assert status.phase == "activated"
    assert inspect_repo_b_deploy_key(active).generation_id == verified.candidate_generation_id


def test_activation_recovers_crash_after_swap_before_completion_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, old, verified = _verify(tmp_path, monkeypatch)
    real = deploy_key_rotation._write_record
    failed = False

    def interrupt(path: Path, record: dict[str, object]) -> None:
        nonlocal failed
        if record["phase"] == "activated" and not failed:
            failed = True
            raise OSError("simulated persistence crash")
        real(path, record)

    monkeypatch.setattr(deploy_key_rotation, "_write_record", interrupt)
    with pytest.raises(DeployKeyRotationError, match="interrupted"):
        activate_repo_b_deploy_key_rotation(active, request_id)
    assert inspect_repo_b_deploy_key(active).generation_id == verified.candidate_generation_id
    assert inspect_repo_b_deploy_key(active.parent / ".repo-b-deploy-key.rotation-retained") == old

    monkeypatch.setattr(deploy_key_rotation, "_write_record", real)
    assert activate_repo_b_deploy_key_rotation(active, request_id).phase == "activated"


def test_activated_replay_performs_no_filesystem_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, _, _ = _verify(tmp_path, monkeypatch)
    first = activate_repo_b_deploy_key_rotation(active, request_id)
    monkeypatch.setattr(deploy_key_rotation, "_rename_directory", pytest.fail)
    monkeypatch.setattr(deploy_key_rotation, "test_repo_b_deploy_key_access", pytest.fail)
    second = activate_repo_b_deploy_key_rotation(active, request_id)
    assert second == first

    third = prepare_repo_b_deploy_key_rotation(active, request_id, TARGET, REPOSITORY_ID)
    assert third == first


def test_prepare_replay_reports_valid_activation_interruption_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, old, _ = _verify(tmp_path, monkeypatch)
    real = deploy_key_rotation._rename_directory
    calls = 0

    def interrupt(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash")
        real(source, destination)

    monkeypatch.setattr(deploy_key_rotation, "_rename_directory", interrupt)
    with pytest.raises(DeployKeyRotationError, match="interrupted"):
        activate_repo_b_deploy_key_rotation(active, request_id)
    monkeypatch.setattr(deploy_key_rotation, "_rename_directory", pytest.fail)

    status = prepare_repo_b_deploy_key_rotation(active, request_id, TARGET, REPOSITORY_ID)
    assert status.phase == "activating"
    assert inspect_repo_b_deploy_key(active.parent / ".repo-b-deploy-key.rotation-retained") == old


def test_existing_retained_generation_prevents_new_rotation(tmp_path: Path) -> None:
    active, _ = _active(tmp_path)
    retained = active.parent / ".repo-b-deploy-key.rotation-retained"
    ensure_repo_b_deploy_key(retained)
    with pytest.raises(DeployKeyRotationError, match="retained"):
        prepare_repo_b_deploy_key_rotation(
            active,
            str(uuid4()),
            TARGET,
            REPOSITORY_ID,
        )


@pytest.mark.parametrize("mutation", ["content", "mode", "symlink", "hardlink"])
def test_rotation_record_tampering_fails_closed(tmp_path: Path, mutation: str) -> None:
    active, request_id, _, _ = _prepare(tmp_path)
    record = active.parent / ".repo-b-deploy-key.rotation.json"
    if mutation == "content":
        value = json.loads(record.read_text())
        value["target"] = "Other/Repo"
        record.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    elif mutation == "mode":
        record.chmod(0o644)
    elif mutation == "symlink":
        outside = tmp_path / "outside"
        outside.write_text(record.read_text())
        record.unlink()
        record.symlink_to(outside)
    else:
        os.link(record, tmp_path / "record-alias")

    with pytest.raises(DeployKeyRotationError, match="record is invalid"):
        inspect_repo_b_deploy_key_rotation(active, request_id)


def test_rotation_identity_and_repository_binding_cannot_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, request_id, _, _ = _prepare(tmp_path)
    monkeypatch.setattr(deploy_key_rotation, "test_repo_b_deploy_key_access", pytest.fail)
    with pytest.raises(DeployKeyRotationError, match="does not match"):
        verify_repo_b_deploy_key_rotation(
            active,
            request_id,
            "Other/Repo",
            TOKEN,
            REPOSITORY_ID,
            work_directory=active.parent,
        )
    with pytest.raises(DeployKeyRotationError, match="does not match"):
        verify_repo_b_deploy_key_rotation(
            active,
            request_id,
            TARGET,
            TOKEN,
            REPOSITORY_ID + 1,
            work_directory=active.parent,
        )


def test_candidate_generation_rebinding_is_rejected(tmp_path: Path) -> None:
    active, request_id, _, _ = _prepare(tmp_path)
    candidate = active.parent / ".repo-b-deploy-key.rotation-candidate"
    moved = tmp_path / "original-candidate"
    candidate.rename(moved)
    ensure_repo_b_deploy_key(candidate)

    with pytest.raises(DeployKeyRotationError, match="generation changed"):
        inspect_repo_b_deploy_key_rotation(active, request_id)


def test_rotation_lock_prevents_concurrent_execution(tmp_path: Path) -> None:
    active, _ = _active(tmp_path)
    request_id = str(uuid4())
    paths = deploy_key_rotation._paths(active)

    with (
        deploy_key_rotation._rotation_lock(paths),
        pytest.raises(DeployKeyRotationError, match="busy") as error,
    ):
        prepare_repo_b_deploy_key_rotation(active, request_id, TARGET, REPOSITORY_ID)
    assert error.value.transient is True


def test_symlinked_parent_is_rejected_before_lock_or_record_creation(tmp_path: Path) -> None:
    active, _ = _active(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(active.parent, target_is_directory=True)
    aliased_active = alias / active.name

    with pytest.raises(DeployKeyRotationError, match="parent is invalid"):
        prepare_repo_b_deploy_key_rotation(
            aliased_active,
            str(uuid4()),
            TARGET,
            REPOSITORY_ID,
        )
    assert {entry.name for entry in active.parent.iterdir()} == {active.name}


def test_duplicate_or_oversized_rotation_record_is_rejected(tmp_path: Path) -> None:
    active, request_id, _, _ = _prepare(tmp_path)
    record = active.parent / ".repo-b-deploy-key.rotation.json"
    raw = record.read_text()
    record.write_text(raw.replace("{", '{"phase":"prepared",', 1))
    with pytest.raises(DeployKeyRotationError, match="record is invalid"):
        inspect_repo_b_deploy_key_rotation(active, request_id)

    record.write_bytes(b"x" * 32_769)
    record.chmod(0o600)
    with pytest.raises(DeployKeyRotationError, match="record is invalid"):
        inspect_repo_b_deploy_key_rotation(active, request_id)


def test_stale_candidate_generation_journal_fails_closed(tmp_path: Path) -> None:
    active, request_id, _, _ = _prepare(tmp_path)
    paths = deploy_key_rotation._paths(active)
    paths.candidate_journal.write_text(
        json.dumps({"schema_version": 1, "generation_id": str(uuid4())}) + "\n"
    )
    paths.candidate_journal.chmod(0o600)

    with pytest.raises(DeployKeyRotationError, match="generation changed"):
        inspect_repo_b_deploy_key_rotation(active, request_id)


def test_hardlinked_rotation_lock_is_rejected(tmp_path: Path) -> None:
    active, request_id, _, _ = _prepare(tmp_path)
    paths = deploy_key_rotation._paths(active)
    os.link(paths.lock, tmp_path / "lock-alias")

    with pytest.raises(DeployKeyRotationError, match="lock is invalid"):
        inspect_repo_b_deploy_key_rotation(active, request_id)
