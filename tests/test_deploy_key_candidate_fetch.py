import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from ha_syncapp import candidate_fetch
from ha_syncapp.candidate_fetch import (
    CandidateFetch,
    CandidateFetchError,
    fetch_trusted_candidate_with_deploy_key,
)
from ha_syncapp.deploy_key import ensure_repo_b_deploy_key
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = "Owner/Home"
REPOSITORY_ID = 42
SHA = "a" * 40
OTHER_SHA = "b" * 40


def _inputs(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, DeployKeyAccessProof, DeployKeyReferenceSnapshot]:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    key_directory = protected / "repo-b-deploy-key"
    enrollment = ensure_repo_b_deploy_key(key_directory)
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir(mode=0o700)
    workspaces.chmod(0o700)
    home = tmp_path / "homeassistant"
    home.mkdir()
    known_hosts = ROOT / "syncapp/github_known_hosts"
    proof = DeployKeyAccessProof(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        key_fingerprint=enrollment.fingerprint,
        generation_id=enrollment.generation_id,
        ref_count=1,
        observation_sha256=hashlib.sha256(b"access-proof").hexdigest(),
    )
    snapshot = DeployKeyReferenceSnapshot(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        key_fingerprint=enrollment.fingerprint,
        generation_id=enrollment.generation_id,
        references=(DeployKeyReference("refs/heads/candidate", SHA),),
        observation_sha256=hashlib.sha256(b"candidate-reference").hexdigest(),
    )
    return key_directory, known_hosts, workspaces, home, proof, snapshot


def _successful_local_git(calls: list[tuple[str, ...]]) -> Callable[..., str]:
    def run(
        _executable: str,
        root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        calls.append(arguments)
        if arguments[0] == "init":
            (root / ".git").mkdir()
            return ""
        if arguments[0] == "rev-parse":
            return SHA
        if arguments[0] == "cat-file":
            return "commit"
        return ""

    return run


def _fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: DeployKeyReferenceSnapshot | None = None,
) -> tuple[CandidateFetch, list[tuple[str, ...]], list[tuple[tuple[str, ...], dict[str, str]]]]:
    key_directory, known_hosts, workspaces, home, proof, current = _inputs(tmp_path)
    observed = current if snapshot is None else snapshot
    monkeypatch.setattr(
        candidate_fetch,
        "read_repo_b_deploy_key_references",
        lambda *args, **kwargs: observed,
    )
    local_calls: list[tuple[str, ...]] = []
    transport_calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    monkeypatch.setattr(candidate_fetch, "_run_git", _successful_local_git(local_calls))

    def transport(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        pass_fds: tuple[int, ...],
    ) -> bytes:
        assert cwd.name.startswith(".git-workspace-candidate-")
        assert len(pass_fds) == 1
        assert stat.S_ISREG(os.fstat(pass_fds[0]).st_mode)
        transport_calls.append((command, environment))
        return b""

    monkeypatch.setattr(candidate_fetch, "run_bounded_repo_b_git", transport)
    result = fetch_trusted_candidate_with_deploy_key(
        proof,
        TARGET,
        REPOSITORY_ID,
        SHA,
        key_directory,
        workspaces,
        home,
        known_hosts_file=known_hosts,
    )
    return result, local_calls, transport_calls


def test_fetches_only_fresh_exact_candidate_ref_with_protected_ssh_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, local_calls, transport_calls = _fetch(tmp_path, monkeypatch)

    assert result.target == TARGET
    assert result.repository_id == REPOSITORY_ID
    assert result.branch == "candidate"
    assert result.commit_sha == SHA
    assert result.git_ref == "refs/syncapp/candidate-fetch"
    assert {entry.name for entry in result.root.iterdir()} == {".git"}
    assert len(transport_calls) == 1
    command, environment = transport_calls[0]
    assert command[-6:] == (
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--depth=1",
        "ssh://git@github.com/Owner/Home.git",
        "refs/heads/candidate:refs/syncapp/candidate-fetch",
    )
    assert "credential.helper=" in command
    assert "protocol.file.allow=never" in command
    assert "fetch.fsckObjects=true" in command
    ssh_config = next(value for value in command if value.startswith("core.sshCommand="))
    assert "IdentitiesOnly=yes" in ssh_config
    assert "StrictHostKeyChecking=yes" in ssh_config
    assert "ClearAllForwardings=yes" in ssh_config
    serialized = json.dumps([command, environment])
    assert "github-token" not in serialized
    assert "GIT_ASKPASS" not in environment
    assert not any(entry.name.startswith(".syncapp") for entry in result.root.iterdir())
    forbidden = {"checkout", "merge", "pull", "push", "reset", "switch"}
    assert not forbidden.intersection(call[0] for call in local_calls)


@pytest.mark.parametrize(
    "references",
    [
        (),
        (DeployKeyReference("refs/heads/main", SHA),),
        (DeployKeyReference("refs/heads/candidate", OTHER_SHA),),
        (
            DeployKeyReference("refs/heads/candidate", SHA),
            DeployKeyReference("refs/heads/candidate", SHA),
        ),
    ],
)
def test_missing_moved_or_rebound_candidate_reference_fails_before_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    references: tuple[DeployKeyReference, ...],
) -> None:
    key_directory, known_hosts, workspaces, home, proof, snapshot = _inputs(tmp_path)
    forged = DeployKeyReferenceSnapshot(
        target=snapshot.target,
        repository_id=snapshot.repository_id,
        key_fingerprint=snapshot.key_fingerprint,
        generation_id=snapshot.generation_id,
        references=references,
        observation_sha256=snapshot.observation_sha256,
    )
    monkeypatch.setattr(
        candidate_fetch,
        "read_repo_b_deploy_key_references",
        lambda *args, **kwargs: forged,
    )
    monkeypatch.setattr(candidate_fetch, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(CandidateFetchError, match="candidate reference") as error:
        fetch_trusted_candidate_with_deploy_key(
            proof,
            TARGET,
            REPOSITORY_ID,
            SHA,
            key_directory,
            workspaces,
            home,
            known_hosts_file=known_hosts,
        )
    assert error.value.transient is False
    assert list(workspaces.iterdir()) == []


def test_repository_or_key_binding_mismatch_fails_before_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, workspaces, home, proof, snapshot = _inputs(tmp_path)
    rebound = DeployKeyReferenceSnapshot(
        target="Other/Repo",
        repository_id=snapshot.repository_id,
        key_fingerprint=snapshot.key_fingerprint,
        generation_id=snapshot.generation_id,
        references=snapshot.references,
        observation_sha256=snapshot.observation_sha256,
    )
    monkeypatch.setattr(
        candidate_fetch,
        "read_repo_b_deploy_key_references",
        lambda *args, **kwargs: rebound,
    )
    monkeypatch.setattr(candidate_fetch, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(CandidateFetchError, match="reference evidence"):
        fetch_trusted_candidate_with_deploy_key(
            proof,
            TARGET,
            REPOSITORY_ID,
            SHA,
            key_directory,
            workspaces,
            home,
            known_hosts_file=known_hosts,
        )
    assert list(workspaces.iterdir()) == []


def test_rotation_between_reference_observation_and_fetch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, workspaces, home, proof, snapshot = _inputs(tmp_path)
    replacement = key_directory.parent / "replacement"
    ensure_repo_b_deploy_key(replacement)

    def observe(*args: object, **kwargs: object) -> DeployKeyReferenceSnapshot:
        for name in ("private_key", "public_key", "manifest.json"):
            (key_directory / name).write_bytes((replacement / name).read_bytes())
        return snapshot

    monkeypatch.setattr(candidate_fetch, "read_repo_b_deploy_key_references", observe)
    monkeypatch.setattr(candidate_fetch, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(CandidateFetchError, match="transport authority") as error:
        fetch_trusted_candidate_with_deploy_key(
            proof,
            TARGET,
            REPOSITORY_ID,
            SHA,
            key_directory,
            workspaces,
            home,
            known_hosts_file=known_hosts,
        )
    assert error.value.transient is False
    assert list(workspaces.iterdir()) == []


def test_transient_ssh_failure_is_sanitized_and_removes_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, workspaces, home, proof, snapshot = _inputs(tmp_path)
    monkeypatch.setattr(
        candidate_fetch,
        "read_repo_b_deploy_key_references",
        lambda *args, **kwargs: snapshot,
    )
    monkeypatch.setattr(candidate_fetch, "_run_git", _successful_local_git([]))

    def unavailable(*args: object, **kwargs: object) -> bytes:
        raise DeployKeyAccessError("secret network diagnostic", transient=True)

    monkeypatch.setattr(candidate_fetch, "run_bounded_repo_b_git", unavailable)
    with pytest.raises(CandidateFetchError, match="temporarily unavailable") as error:
        fetch_trusted_candidate_with_deploy_key(
            proof,
            TARGET,
            REPOSITORY_ID,
            SHA,
            key_directory,
            workspaces,
            home,
            known_hosts_file=known_hosts,
        )
    assert error.value.transient is True
    assert "secret network diagnostic" not in str(error.value)
    assert list(workspaces.iterdir()) == []


def test_fetched_object_mismatch_is_deterministic_and_removes_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_directory, known_hosts, workspaces, home, proof, snapshot = _inputs(tmp_path)
    monkeypatch.setattr(
        candidate_fetch,
        "read_repo_b_deploy_key_references",
        lambda *args, **kwargs: snapshot,
    )

    def local(_executable: str, root: Path, arguments: tuple[str, ...], **kwargs: object) -> str:
        if arguments[0] == "init":
            (root / ".git").mkdir()
        if arguments[0] == "rev-parse":
            return OTHER_SHA
        if arguments[0] == "cat-file":
            return "commit"
        return ""

    monkeypatch.setattr(candidate_fetch, "_run_git", local)
    monkeypatch.setattr(candidate_fetch, "run_bounded_repo_b_git", lambda *args, **kwargs: b"")
    with pytest.raises(CandidateFetchError, match="does not match") as error:
        fetch_trusted_candidate_with_deploy_key(
            proof,
            TARGET,
            REPOSITORY_ID,
            SHA,
            key_directory,
            workspaces,
            home,
            known_hosts_file=known_hosts,
        )
    assert error.value.transient is False
    assert list(workspaces.iterdir()) == []
