import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from ha_syncapp import publication_transport
from ha_syncapp.deploy_key import ensure_repo_b_deploy_key
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from ha_syncapp.git_workspace import GitWorkspace, prepare_git_workspace
from ha_syncapp.local_git import create_snapshot_commit, initialize_repository
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.publication_transport import (
    PublicationTransportError,
    push_publication_intent_with_deploy_key,
)
from ha_syncapp.snapshot import capture_snapshot

ROOT = Path(__file__).resolve().parents[1]
TARGET = "Owner/Home"
REPOSITORY_ID = 42
BASELINE = "a" * 40


def _workspace(tmp_path: Path) -> tuple[GitWorkspace, str]:
    source = tmp_path / "ha"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    source.mkdir()
    snapshots.mkdir()
    workspaces.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    snapshot = capture_snapshot(source, snapshots)
    workspace = prepare_git_workspace(snapshot.root, workspaces)
    initialize_repository(workspace)
    commit_sha = create_snapshot_commit(workspace)
    assert commit_sha is not None
    return workspace, commit_sha


def _authority(tmp_path: Path) -> tuple[Path, Path, DeployKeyAccessProof]:
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    key_directory = protected / "repo-b-deploy-key"
    enrollment = ensure_repo_b_deploy_key(key_directory)
    proof = DeployKeyAccessProof(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        key_fingerprint=enrollment.fingerprint,
        generation_id=enrollment.generation_id,
        ref_count=1,
        observation_sha256=hashlib.sha256(b"access-proof").hexdigest(),
    )
    return key_directory, ROOT / "syncapp/github_known_hosts", proof


def _snapshot(
    proof: DeployKeyAccessProof,
    references: tuple[DeployKeyReference, ...],
    *,
    target: str = TARGET,
    repository_id: int = REPOSITORY_ID,
) -> DeployKeyReferenceSnapshot:
    return DeployKeyReferenceSnapshot(
        target=target,
        repository_id=repository_id,
        key_fingerprint=proof.key_fingerprint,
        generation_id=proof.generation_id,
        references=references,
        observation_sha256=hashlib.sha256(repr(references).encode()).hexdigest(),
    )


def _intent(commit_sha: str, *, initial: bool = False) -> PublicationIntent:
    return PublicationIntent(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="main",
        local_commit_sha=commit_sha,
        expected_remote_commit_sha=None if initial else BASELINE,
        expect_remote_absent=initial,
    )


def _install_reference_reads(
    monkeypatch: pytest.MonkeyPatch,
    snapshots: list[DeployKeyReferenceSnapshot],
) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []
    queued = iter(snapshots)

    def read(*args: object, **kwargs: object) -> DeployKeyReferenceSnapshot:
        calls.append((*args, kwargs))
        return next(queued)

    monkeypatch.setattr(publication_transport, "read_repo_b_deploy_key_references", read)
    return calls


def test_pushes_only_exact_authorized_ref_over_descriptor_bound_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, (DeployKeyReference("refs/heads/main", BASELINE),))
    after = _snapshot(proof, (DeployKeyReference("refs/heads/main", commit_sha),))
    reads = _install_reference_reads(monkeypatch, [before, after])
    transports: list[tuple[tuple[str, ...], dict[str, str], tuple[int, ...]]] = []

    def run_transport(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        pass_fds: tuple[int, ...],
    ) -> bytes:
        assert cwd == workspace.tree_path
        assert len(pass_fds) == 1
        assert stat.S_ISREG(os.fstat(pass_fds[0]).st_mode)
        transports.append((command, environment, pass_fds))
        return b""

    monkeypatch.setattr(publication_transport, "run_bounded_repo_b_git", run_transport)

    published = push_publication_intent_with_deploy_key(
        workspace,
        _intent(commit_sha),
        proof,
        key_directory,
        known_hosts_file=known_hosts,
    )

    assert published == commit_sha
    assert len(reads) == 2
    assert len(transports) == 1
    command, environment, _descriptors = transports[0]
    assert command[-5:] == (
        "push",
        "--porcelain",
        "--no-verify",
        "ssh://git@github.com/Owner/Home.git",
        f"{commit_sha}:refs/heads/main",
    )
    assert not any(argument.startswith("--force") for argument in command)
    assert "credential.helper=" in command
    assert "protocol.file.allow=never" in command
    assert "push.recurseSubmodules=no" in command
    ssh_config = next(value for value in command if value.startswith("core.sshCommand="))
    assert "IdentitiesOnly=yes" in ssh_config
    assert "StrictHostKeyChecking=yes" in ssh_config
    serialized = json.dumps([command, environment])
    assert "github-token" not in serialized
    assert "GIT_ASKPASS" not in environment
    assert not (workspace.root / ".syncapp-push-askpass").exists()


def test_initial_publication_requires_absent_branch_and_verifies_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, ())
    after = _snapshot(proof, (DeployKeyReference("refs/heads/main", commit_sha),))
    _install_reference_reads(monkeypatch, [before, after])
    monkeypatch.setattr(
        publication_transport, "run_bounded_repo_b_git", lambda *args, **kwargs: b""
    )

    assert (
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha, initial=True),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )
        == commit_sha
    )


@pytest.mark.parametrize(
    "references",
    [
        (),
        (DeployKeyReference("refs/heads/main", "b" * 40),),
        (
            DeployKeyReference("refs/heads/main", BASELINE),
            DeployKeyReference("refs/heads/main", BASELINE),
        ),
    ],
)
def test_missing_moved_or_duplicate_baseline_fails_before_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    references: tuple[DeployKeyReference, ...],
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    _install_reference_reads(monkeypatch, [_snapshot(proof, references)])
    monkeypatch.setattr(publication_transport, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(PublicationTransportError, match="(?:branch|reference) evidence") as error:
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is False


def test_initial_publication_rejects_existing_branch_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    existing = _snapshot(proof, (DeployKeyReference("refs/heads/main", BASELINE),))
    _install_reference_reads(monkeypatch, [existing])
    monkeypatch.setattr(publication_transport, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(PublicationTransportError, match="no longer absent"):
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha, initial=True),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )


def test_rebound_reference_snapshot_fails_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    rebound = _snapshot(
        proof,
        (DeployKeyReference("refs/heads/main", BASELINE),),
        target="Other/Repo",
    )
    _install_reference_reads(monkeypatch, [rebound])
    monkeypatch.setattr(publication_transport, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(PublicationTransportError, match="reference evidence"):
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )


def test_key_generation_change_before_push_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, (DeployKeyReference("refs/heads/main", BASELINE),))
    _install_reference_reads(monkeypatch, [before])

    @contextmanager
    def changed(*_args: object, **_kwargs: object) -> Iterator[object]:
        raise DeployKeyAccessError("changed generation")
        yield

    monkeypatch.setattr(publication_transport, "open_repo_b_deploy_key_transport", changed)
    monkeypatch.setattr(publication_transport, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(PublicationTransportError, match="authority is invalid") as error:
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is False


def test_transient_transport_failure_remains_retryable_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, (DeployKeyReference("refs/heads/main", BASELINE),))
    _install_reference_reads(monkeypatch, [before])
    secret = "private-key-secret-sentinel"

    def unavailable(*_args: object, **_kwargs: object) -> bytes:
        raise DeployKeyAccessError(secret, transient=True)

    monkeypatch.setattr(publication_transport, "run_bounded_repo_b_git", unavailable)

    with pytest.raises(PublicationTransportError) as error:
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is True
    assert secret not in str(error.value)


def test_unobserved_push_outcome_is_transient_for_safe_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    baseline = _snapshot(proof, (DeployKeyReference("refs/heads/main", BASELINE),))
    _install_reference_reads(monkeypatch, [baseline, baseline])
    monkeypatch.setattr(
        publication_transport, "run_bounded_repo_b_git", lambda *args, **kwargs: b""
    )

    with pytest.raises(PublicationTransportError, match="could not be confirmed") as error:
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is True


def test_post_push_divergence_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, (DeployKeyReference("refs/heads/main", BASELINE),))
    after = _snapshot(proof, (DeployKeyReference("refs/heads/main", "b" * 40),))
    _install_reference_reads(monkeypatch, [before, after])
    monkeypatch.setattr(
        publication_transport, "run_bounded_repo_b_git", lambda *args, **kwargs: b""
    )

    with pytest.raises(PublicationTransportError, match="diverged") as error:
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is False


def test_workspace_drift_during_transport_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, (DeployKeyReference("refs/heads/main", BASELINE),))
    after = _snapshot(proof, (DeployKeyReference("refs/heads/main", commit_sha),))
    _install_reference_reads(monkeypatch, [before, after])

    def mutate(*_args: object, **_kwargs: object) -> bytes:
        (workspace.tree_path / "configuration.yaml").write_text("homeassistant:\n  name: changed\n")
        return b""

    monkeypatch.setattr(publication_transport, "run_bounded_repo_b_git", mutate)

    with pytest.raises(PublicationTransportError, match="changed during transport"):
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )


def test_local_git_url_rewrite_cannot_redirect_authorized_ssh_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, (DeployKeyReference("refs/heads/main", BASELINE),))
    _install_reference_reads(monkeypatch, [before])
    subprocess.run(
        [
            "/usr/bin/git",
            "config",
            "--local",
            "url.ssh://attacker.invalid/.pushInsteadOf",
            "ssh://git@github.com/",
        ],
        cwd=workspace.tree_path,
        check=True,
    )
    monkeypatch.setattr(publication_transport, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(PublicationTransportError, match="metadata is unsafe"):
        push_publication_intent_with_deploy_key(
            workspace,
            _intent(commit_sha),
            proof,
            key_directory,
            known_hosts_file=known_hosts,
        )
