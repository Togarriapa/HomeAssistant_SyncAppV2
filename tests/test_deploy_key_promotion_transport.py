from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp import deployment_promotion_transport as transport
from ha_syncapp.candidate_fetch import CandidateFetch, CandidateFetchError
from ha_syncapp.deploy_key import ensure_repo_b_deploy_key
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from ha_syncapp.deployment_promotion import DeploymentPromotion, PromotionRemoteState
from ha_syncapp.deployment_promotion_transport import (
    DeploymentPromotionTransportError,
    publish_promotion_refs_with_deploy_key,
    read_promotion_remote_state_with_deploy_key,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = "Owner/Home"
REPOSITORY_ID = 42
BASELINE = "a" * 40
OTHER = "c" * 40


def _intent(candidate: str) -> DeploymentPromotion:
    return DeploymentPromotion.create(
        deployment_id="11111111-1111-4111-8111-111111111111",
        target=TARGET,
        repository_id=REPOSITORY_ID,
        candidate_sha=candidate,
        baseline_sha=BASELINE,
        backup_slug="backup-1",
        finalization_sha256="f" * 64,
        known_good_tag="syncapp-known-good-11111111-1111-4111-8111-111111111111",
        phase="planned",
        block_reason="none",
        planned_at=datetime(2026, 9, 29, tzinfo=UTC),
        terminal_at=None,
    )


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
        ref_count=3,
        observation_sha256=hashlib.sha256(b"promotion-access-proof").hexdigest(),
    )
    return key_directory, ROOT / "syncapp/github_known_hosts", proof


def _workspace(tmp_path: Path) -> tuple[Path, Path, str, CandidateFetch]:
    home = tmp_path / "home-assistant"
    workspace_root = tmp_path / "workspaces"
    home.mkdir(mode=0o700)
    workspace_root.mkdir(mode=0o700)
    root = workspace_root / ".git-workspace-candidate-test.tmp"
    root.mkdir(mode=0o700)
    subprocess.run(["/usr/bin/git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(root), "commit", "--allow-empty", "-m", "candidate"],
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "SyncApp Test",
            "GIT_AUTHOR_EMAIL": "syncapp@localhost",
            "GIT_COMMITTER_NAME": "SyncApp Test",
            "GIT_COMMITTER_EMAIL": "syncapp@localhost",
        },
        capture_output=True,
    )
    candidate = subprocess.run(
        ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(root),
            "update-ref",
            "refs/syncapp/candidate-fetch",
            candidate,
        ],
        check=True,
    )
    return (
        workspace_root,
        home,
        candidate,
        CandidateFetch(root, TARGET, REPOSITORY_ID, "candidate", candidate, "refs/syncapp/candidate-fetch"),
    )


def _snapshot(
    proof: DeployKeyAccessProof,
    candidate: str,
    main: str,
    tag: str | None,
    *,
    target: str = TARGET,
    generation: str | None = None,
) -> DeployKeyReferenceSnapshot:
    intent = _intent(candidate)
    refs = [
        DeployKeyReference("refs/heads/candidate", candidate),
        DeployKeyReference("refs/heads/main", main),
    ]
    if tag is not None:
        refs.append(DeployKeyReference(f"refs/tags/{intent.known_good_tag}", tag))
    refs.sort(key=lambda value: value.name)
    references = tuple(refs)
    return DeployKeyReferenceSnapshot(
        target=target,
        repository_id=REPOSITORY_ID,
        key_fingerprint=proof.key_fingerprint,
        generation_id=generation or proof.generation_id,
        references=references,
        observation_sha256=hashlib.sha256(repr(references).encode()).hexdigest(),
    )


def _install_reads(
    monkeypatch: pytest.MonkeyPatch,
    snapshots: list[DeployKeyReferenceSnapshot],
) -> list[tuple[object, ...]]:
    queued = iter(snapshots)
    calls: list[tuple[object, ...]] = []

    def read(*args: object, **kwargs: object) -> DeployKeyReferenceSnapshot:
        calls.append((*args, kwargs))
        return next(queued)

    monkeypatch.setattr(transport, "read_repo_b_deploy_key_references", read)
    return calls


def _install_fetch(
    monkeypatch: pytest.MonkeyPatch, fetched: CandidateFetch
) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    def fetch(*args: object, **kwargs: object) -> CandidateFetch:
        calls.append((*args, kwargs))
        return fetched

    monkeypatch.setattr(transport, "fetch_trusted_candidate_with_deploy_key", fetch)
    return calls


def test_reads_exact_candidate_main_and_optional_tag_from_proof_bound_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root, _home, candidate, _fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    _install_reads(monkeypatch, [_snapshot(proof, candidate, BASELINE, None)])

    state = read_promotion_remote_state_with_deploy_key(
        proof,
        TARGET,
        REPOSITORY_ID,
        _intent(candidate).known_good_tag,
        key_directory,
        work_directory=workspace_root,
        known_hosts_file=known_hosts,
    )

    assert state == PromotionRemoteState(candidate, BASELINE, None)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot, _proof: snapshot.__class__(
            snapshot.target,
            snapshot.repository_id,
            snapshot.key_fingerprint,
            snapshot.generation_id,
            tuple(ref for ref in snapshot.references if ref.name != "refs/heads/main"),
            snapshot.observation_sha256,
        ),
        lambda snapshot, _proof: snapshot.__class__(
            snapshot.target,
            snapshot.repository_id,
            snapshot.key_fingerprint,
            snapshot.generation_id,
            (*snapshot.references, snapshot.references[0]),
            snapshot.observation_sha256,
        ),
        lambda snapshot, _proof: snapshot.__class__(
            "Other/Home",
            snapshot.repository_id,
            snapshot.key_fingerprint,
            snapshot.generation_id,
            snapshot.references,
            snapshot.observation_sha256,
        ),
    ],
)
def test_missing_duplicate_or_rebound_ref_evidence_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    workspace_root, _home, candidate, _fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    snapshot = mutate(_snapshot(proof, candidate, BASELINE, None), proof)
    _install_reads(monkeypatch, [snapshot])

    with pytest.raises(DeploymentPromotionTransportError) as error:
        read_promotion_remote_state_with_deploy_key(
            proof,
            TARGET,
            REPOSITORY_ID,
            _intent(candidate).known_good_tag,
            key_directory,
            work_directory=workspace_root,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is False


def test_both_missing_refs_are_published_in_one_atomic_non_force_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root, home, candidate, fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    intent = _intent(candidate)
    before = _snapshot(proof, candidate, BASELINE, None)
    completed = _snapshot(proof, candidate, candidate, candidate)
    reads = _install_reads(monkeypatch, [before, before, completed])
    fetches = _install_fetch(monkeypatch, fetched)
    pushes: list[tuple[tuple[str, ...], dict[str, str], tuple[int, ...]]] = []

    def run(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        pass_fds: tuple[int, ...],
    ) -> bytes:
        assert cwd == fetched.root
        assert len(pass_fds) == 1
        assert stat.S_ISREG(os.fstat(pass_fds[0]).st_mode)
        pushes.append((command, environment, pass_fds))
        return b""

    monkeypatch.setattr(transport, "run_bounded_repo_b_git", run)

    publish_promotion_refs_with_deploy_key(
        intent,
        PromotionRemoteState(candidate, BASELINE, None),
        proof,
        key_directory,
        workspace_root,
        home,
        known_hosts_file=known_hosts,
    )

    assert len(reads) == 3
    assert len(fetches) == 1
    assert len(pushes) == 1
    command, environment, _descriptors = pushes[0]
    assert command[-8:] == (
        "push",
        "--atomic",
        "--porcelain",
        "--no-verify",
        "ssh://git@github.com/Owner/Home.git",
        "refs/syncapp/candidate-fetch:refs/heads/main",
        f"refs/syncapp/candidate-fetch:refs/tags/{intent.known_good_tag}",
    )[-8:]
    assert not any("force" in argument or "delete" in argument or "mirror" in argument for argument in command)
    assert "credential.helper=" in command
    assert "protocol.file.allow=never" in command
    assert "push.recurseSubmodules=no" in command
    serialized = json.dumps([command, environment])
    assert "github-token" not in serialized
    assert "GIT_ASKPASS" not in environment
    assert not fetched.root.exists()


@pytest.mark.parametrize(
    ("main", "tag", "expected_ref"),
    [
        (BASELINE, "candidate", "refs/heads/main"),
        ("candidate", None, "refs/tags/"),
    ],
)
def test_partial_publication_pushes_only_the_missing_authorized_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    main: str,
    tag: str | None,
    expected_ref: str,
) -> None:
    workspace_root, home, candidate, fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    actual_main = candidate if main == "candidate" else main
    actual_tag = candidate if tag == "candidate" else tag
    before = _snapshot(proof, candidate, actual_main, actual_tag)
    completed = _snapshot(proof, candidate, candidate, candidate)
    _install_reads(monkeypatch, [before, before, completed])
    _install_fetch(monkeypatch, fetched)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        transport,
        "run_bounded_repo_b_git",
        lambda command, **_kwargs: commands.append(command) or b"",
    )

    publish_promotion_refs_with_deploy_key(
        _intent(candidate),
        PromotionRemoteState(candidate, actual_main, actual_tag),
        proof,
        key_directory,
        workspace_root,
        home,
        known_hosts_file=known_hosts,
    )

    refspecs = [argument for argument in commands[0] if argument.startswith("refs/syncapp/")]
    assert len(refspecs) == 1
    assert expected_ref in refspecs[0]
    assert not fetched.root.exists()


def test_completed_state_is_an_idempotent_noop_without_fetch_or_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root, home, candidate, fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    completed = _snapshot(proof, candidate, candidate, candidate)
    reads = _install_reads(monkeypatch, [completed])
    monkeypatch.setattr(transport, "fetch_trusted_candidate_with_deploy_key", pytest.fail)
    monkeypatch.setattr(transport, "run_bounded_repo_b_git", pytest.fail)

    publish_promotion_refs_with_deploy_key(
        _intent(candidate),
        PromotionRemoteState(candidate, candidate, candidate),
        proof,
        key_directory,
        workspace_root,
        home,
        known_hosts_file=known_hosts,
    )

    assert len(reads) == 1
    assert fetched.root.exists()


def test_stale_supplied_state_fails_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root, home, candidate, _fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    _install_reads(monkeypatch, [_snapshot(proof, candidate, candidate, None)])
    monkeypatch.setattr(transport, "fetch_trusted_candidate_with_deploy_key", pytest.fail)

    with pytest.raises(DeploymentPromotionTransportError, match="changed") as error:
        publish_promotion_refs_with_deploy_key(
            _intent(candidate),
            PromotionRemoteState(candidate, BASELINE, None),
            proof,
            key_directory,
            workspace_root,
            home,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is False


def test_pre_push_generation_drift_blocks_without_mutation_and_cleans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root, home, candidate, fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, candidate, BASELINE, None)
    drifted = _snapshot(proof, candidate, BASELINE, None, generation="2" * 8 + "-2222-4222-8222-222222222222")
    _install_reads(monkeypatch, [before, drifted])
    _install_fetch(monkeypatch, fetched)
    monkeypatch.setattr(transport, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(DeploymentPromotionTransportError):
        publish_promotion_refs_with_deploy_key(
            _intent(candidate),
            PromotionRemoteState(candidate, BASELINE, None),
            proof,
            key_directory,
            workspace_root,
            home,
            known_hosts_file=known_hosts,
        )

    assert not fetched.root.exists()


@pytest.mark.parametrize(
    ("after_main", "after_tag", "transient"),
    [
        (BASELINE, None, True),
        ("candidate", None, True),
        (OTHER, None, False),
    ],
)
def test_post_push_uncertainty_is_retryable_but_conflict_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_main: str,
    after_tag: str | None,
    transient: bool,
) -> None:
    workspace_root, home, candidate, fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, candidate, BASELINE, None)
    actual_main = candidate if after_main == "candidate" else after_main
    actual_tag = candidate if after_tag == "candidate" else after_tag
    after = _snapshot(proof, candidate, actual_main, actual_tag)
    _install_reads(monkeypatch, [before, before, after])
    _install_fetch(monkeypatch, fetched)
    monkeypatch.setattr(transport, "run_bounded_repo_b_git", lambda *args, **kwargs: b"")

    with pytest.raises(DeploymentPromotionTransportError) as error:
        publish_promotion_refs_with_deploy_key(
            _intent(candidate),
            PromotionRemoteState(candidate, BASELINE, None),
            proof,
            key_directory,
            workspace_root,
            home,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is transient
    assert not fetched.root.exists()


def test_unsafe_local_url_rewrite_is_rejected_before_push_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root, home, candidate, fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, candidate, BASELINE, None)
    _install_reads(monkeypatch, [before, before])
    _install_fetch(monkeypatch, fetched)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(fetched.root),
            "config",
            "url.file:///tmp/evil.insteadOf",
            "ssh://git@github.com/",
        ],
        check=True,
    )
    monkeypatch.setattr(transport, "run_bounded_repo_b_git", pytest.fail)

    with pytest.raises(DeploymentPromotionTransportError, match="metadata"):
        publish_promotion_refs_with_deploy_key(
            _intent(candidate),
            PromotionRemoteState(candidate, BASELINE, None),
            proof,
            key_directory,
            workspace_root,
            home,
            known_hosts_file=known_hosts,
        )

    assert not fetched.root.exists()


@pytest.mark.parametrize("transient", [False, True])
def test_fetch_failures_are_sanitized_and_preserve_retry_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient: bool
) -> None:
    workspace_root, home, candidate, _fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, candidate, BASELINE, None)
    _install_reads(monkeypatch, [before])

    def fail(*_args: object, **_kwargs: object) -> CandidateFetch:
        raise CandidateFetchError("secret upstream details", transient=transient)

    monkeypatch.setattr(transport, "fetch_trusted_candidate_with_deploy_key", fail)

    with pytest.raises(DeploymentPromotionTransportError) as error:
        publish_promotion_refs_with_deploy_key(
            _intent(candidate),
            PromotionRemoteState(candidate, BASELINE, None),
            proof,
            key_directory,
            workspace_root,
            home,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is transient
    assert "secret" not in str(error.value)


def test_transient_push_failure_is_sanitized_and_workspace_is_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root, home, candidate, fetched = _workspace(tmp_path)
    key_directory, known_hosts, proof = _authority(tmp_path)
    before = _snapshot(proof, candidate, BASELINE, None)
    _install_reads(monkeypatch, [before, before])
    _install_fetch(monkeypatch, fetched)

    def fail(*_args: object, **_kwargs: object) -> bytes:
        raise DeployKeyAccessError("secret transport details", transient=True)

    monkeypatch.setattr(transport, "run_bounded_repo_b_git", fail)

    with pytest.raises(DeploymentPromotionTransportError) as error:
        publish_promotion_refs_with_deploy_key(
            _intent(candidate),
            PromotionRemoteState(candidate, BASELINE, None),
            proof,
            key_directory,
            workspace_root,
            home,
            known_hosts_file=known_hosts,
        )

    assert error.value.transient is True
    assert "secret" not in str(error.value)
    assert not fetched.root.exists()
