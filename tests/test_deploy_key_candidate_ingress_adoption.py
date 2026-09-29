from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp import candidate_deploy_key_ingress as ingress_module
from ha_syncapp.candidate_detection import CandidateObservation
from ha_syncapp.candidate_fetch import CandidateFetch
from ha_syncapp.candidate_fetch_stage_execution import execute_candidate_fetch_stage_once
from ha_syncapp.candidate_orchestration import register_claimed_candidate
from ha_syncapp.config import Config
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from ha_syncapp.state import StateStore

TARGET = "Owner/Home"
REPOSITORY_ID = 123
SHA = "a" * 40
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
PROOF = DeployKeyAccessProof(
    target=TARGET,
    repository_id=REPOSITORY_ID,
    key_fingerprint="SHA256:" + "A" * 43,
    generation_id="123e4567-e89b-42d3-a456-426614174000",
    ref_count=2,
    observation_sha256="b" * 64,
)


def _private_roots(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    key = tmp_path / "key"
    access = tmp_path / "access"
    workspace = tmp_path / "workspace"
    home = tmp_path / "homeassistant"
    for path in (key, access, workspace, home):
        path.mkdir(mode=0o700)
    return key, access, workspace, home


def test_ingress_observes_candidate_and_fetches_with_exact_proof_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, access, workspace, home = _private_roots(tmp_path)
    calls: list[tuple[str, object]] = []
    snapshot = DeployKeyReferenceSnapshot(
        TARGET,
        REPOSITORY_ID,
        PROOF.key_fingerprint,
        PROOF.generation_id,
        (
            DeployKeyReference("refs/heads/main", "c" * 40),
            DeployKeyReference("refs/heads/candidate", SHA),
        ),
        "d" * 64,
    )

    def read_refs(*args: object, **kwargs: object) -> DeployKeyReferenceSnapshot:
        calls.append(("observe", (args, kwargs)))
        return snapshot

    def fetch(*args: object, **kwargs: object) -> CandidateFetch:
        calls.append(("fetch", (args, kwargs)))
        return CandidateFetch(
            workspace / "result",
            TARGET,
            REPOSITORY_ID,
            "candidate",
            SHA,
            "refs/syncapp/candidate-fetch",
        )

    monkeypatch.setattr(ingress_module, "read_repo_b_deploy_key_references", read_refs)
    monkeypatch.setattr(ingress_module, "fetch_trusted_candidate_with_deploy_key", fetch)
    ingress = ingress_module.DeployKeyCandidateIngress(PROOF, key, access)

    observation = ingress.observe(TARGET, expected_id=REPOSITORY_ID)
    result = ingress.fetch(observation, SHA, workspace, home)

    assert observation == CandidateObservation(TARGET, REPOSITORY_ID, "candidate", SHA)
    assert result.commit_sha == SHA
    assert calls[0][1][0][:4] == (PROOF, TARGET, REPOSITORY_ID, key)
    assert calls[1][1][0][:5] == (PROOF, TARGET, REPOSITORY_ID, SHA, key)
    assert "token" not in repr(calls)


def test_ingress_rejects_rebound_snapshot_before_candidate_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, access, workspace, home = _private_roots(tmp_path)
    monkeypatch.setattr(
        ingress_module,
        "read_repo_b_deploy_key_references",
        lambda *args, **kwargs: DeployKeyReferenceSnapshot(
            "Other/Repo",
            REPOSITORY_ID,
            PROOF.key_fingerprint,
            PROOF.generation_id,
            (DeployKeyReference("refs/heads/candidate", SHA),),
            "d" * 64,
        ),
    )
    monkeypatch.setattr(
        ingress_module,
        "fetch_trusted_candidate_with_deploy_key",
        pytest.fail,
    )
    ingress = ingress_module.DeployKeyCandidateIngress(PROOF, key, access)

    with pytest.raises(ingress_module.CandidateDeployKeyIngressError, match="invalid"):
        ingress.observe(TARGET, expected_id=REPOSITORY_ID)


def test_service_builds_deploy_key_ingress_only_after_initialized_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    protected = data / "syncapp"
    protected.mkdir(mode=0o700)
    key = protected / "repo-b-deploy-key"
    key.mkdir(mode=0o700)
    config = Config(
        repo_b=TARGET,
        github_token="rest-identity-token",
        repo_b_candidate_transport="deploy_key",
    )
    observed: list[tuple[object, ...]] = []

    def prove(*args: object, **kwargs: object) -> DeployKeyAccessProof:
        observed.append((args, kwargs))
        return PROOF

    monkeypatch.setattr(service, "test_repo_b_deploy_key_access", prove)
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        with pytest.raises(service.CandidateDetectionServiceError, match="initialized"):
            service._candidate_deploy_key_ingress_if_configured(store, config, data)
        store.record_synchronization_baseline(
            TARGET, "main", "e" * 64, "c" * 40, synchronized_at=NOW
        )
        ingress = service._candidate_deploy_key_ingress_if_configured(store, config, data)

    assert ingress is not None
    assert observed[0][0][:4] == (
        TARGET,
        "rest-identity-token",
        REPOSITORY_ID,
        key,
    )
    assert "rest-identity-token" not in repr(ingress)


def test_candidate_service_uses_deploy_key_ingress_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    key, access, _workspace, _home = _private_roots(tmp_path)
    ingress = ingress_module.DeployKeyCandidateIngress(PROOF, key, access)
    calls: list[tuple[object, ...]] = []
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        monkeypatch.setattr(
            "ha_syncapp.candidate_detection_service.detect_and_enqueue_deploy_key_candidate",
            lambda *args: calls.append(args),
        )
        detector = service.CandidateDetectionService(
            store,
            TARGET,
            None,
            deploy_key_ingress=ingress,
            interval_seconds=60.0,
        )
        detector.start(0.0)
        detector.tick(60.0)

    assert calls == [(store, TARGET, ingress)]


def test_fetch_stage_uses_only_selected_deploy_key_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    key, access, workspace, home = _private_roots(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    ingress = ingress_module.DeployKeyCandidateIngress(PROOF, key, access)
    calls: list[str] = []
    monkeypatch.setattr(
        ingress,
        "observe",
        lambda *args, **kwargs: calls.append("observe")
        or CandidateObservation(TARGET, REPOSITORY_ID, "candidate", SHA),
    )
    monkeypatch.setattr(
        ingress,
        "fetch",
        lambda *args, **kwargs: calls.append("fetch")
        or CandidateFetch(
            workspace / "result",
            TARGET,
            REPOSITORY_ID,
            "candidate",
            SHA,
            "refs/syncapp/candidate-fetch",
        ),
    )
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        store.enqueue_work("candidate", SHA, now=NOW)
        claimed = store.claim_work_kind("candidate", now=NOW)
        assert claimed is not None
        orchestration = register_claimed_candidate(
            store, claimed, target=TARGET, repository_id=REPOSITORY_ID, now=NOW
        )
        with pytest.raises(Exception):
            # The fake fetch has no Git tree; reaching staging proves transport selection.
            execute_candidate_fetch_stage_once(
                store,
                orchestration,
                token=None,
                deploy_key_ingress=ingress,
                workspace_root=workspace,
                staging_root=staging,
                home_assistant_root=home,
                now=NOW,
            )

    assert calls == ["observe", "fetch"]
