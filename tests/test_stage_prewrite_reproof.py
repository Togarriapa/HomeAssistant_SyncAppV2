from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ha_syncapp.apply_authorization import authorize_candidate_apply
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.candidate_stage import CandidateStage
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.preapply_freshness import reprove_preapply_repo_heads
from ha_syncapp.prepared_deployment import PreparedDeployment
from ha_syncapp.stage_prewrite_reproof import (
    StagePrewriteEvidence,
    StagePrewriteReproofError,
    reprove_stage_for_apply,
)


def _backup() -> CandidateBackupEvidence:
    return CandidateBackupEvidence(
        target="owner/private-repo",
        repository_id=12345,
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
        stage_manifest_sha256="c" * 64,
        runtime_sha256="d" * 64,
        risk_level="high",
        core_version="2026.9.1",
        backup_slug="backup_123",
    )


def _prepared() -> PreparedDeployment:
    return PreparedDeployment(str(uuid4()), _backup(), datetime.now(UTC))


def _authorization(prepared: PreparedDeployment):
    evidence = prepared.evidence

    def fetcher(target, token, *, expected_id, branch="main"):
        sha = evidence.baseline_sha if branch == "main" else evidence.candidate_sha
        return BranchHead(target, expected_id, branch, sha)

    freshness = reprove_preapply_repo_heads(
        prepared,
        evidence,
        token="secret-token",
        head_fetcher=fetcher,
    )
    return authorize_candidate_apply(prepared, evidence, freshness)


def _stage(tmp_path: Path, prepared: PreparedDeployment) -> CandidateStage:
    evidence = prepared.evidence
    root = tmp_path / "candidate-stage"
    return CandidateStage(
        root=root,
        tree=root / "tree",
        manifest=root / "manifest.json",
        manifest_sha256=evidence.stage_manifest_sha256,
        target=evidence.target,
        repository_id=evidence.repository_id,
        branch="candidate",
        commit_sha=evidence.candidate_sha,
        entries=(),
    )


def test_exact_authorized_stage_is_reverified_and_returns_immutable_evidence(
    tmp_path, monkeypatch
):
    prepared = _prepared()
    authorization = _authorization(prepared)
    stage = _stage(tmp_path, prepared)
    verified = []

    def verifier(value):
        verified.append(value)

    monkeypatch.setattr("ha_syncapp.stage_prewrite_reproof.verify_candidate_stage", verifier)

    result = reprove_stage_for_apply(authorization, stage)

    assert verified == [stage]
    assert result.deployment_id == authorization.deployment_id
    assert result.target == authorization.target
    assert result.repository_id == authorization.repository_id
    assert result.candidate_sha == authorization.candidate_sha
    assert result.stage_manifest_sha256 == authorization.stage_manifest_sha256
    with pytest.raises(FrozenInstanceError):
        result.candidate_sha = "e" * 40  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target", "other/private-repo"),
        ("repository_id", 54321),
        ("commit_sha", "e" * 40),
        ("manifest_sha256", "f" * 64),
        ("branch", "main"),
    ],
)
def test_stage_binding_mismatch_fails_closed_before_verification(
    tmp_path, monkeypatch, field, value
):
    prepared = _prepared()
    authorization = _authorization(prepared)
    stage = replace(_stage(tmp_path, prepared), **{field: value})
    called = False

    def verifier(_value):
        nonlocal called
        called = True

    monkeypatch.setattr("ha_syncapp.stage_prewrite_reproof.verify_candidate_stage", verifier)

    with pytest.raises(StagePrewriteReproofError, match="binding"):
        reprove_stage_for_apply(authorization, stage)

    assert called is False


def test_stage_integrity_failure_is_sanitized(tmp_path, monkeypatch):
    prepared = _prepared()
    authorization = _authorization(prepared)
    stage = _stage(tmp_path, prepared)

    def verifier(_value):
        raise RuntimeError("secret candidate bytes / token detail")

    monkeypatch.setattr("ha_syncapp.stage_prewrite_reproof.verify_candidate_stage", verifier)

    with pytest.raises(StagePrewriteReproofError) as caught:
        reprove_stage_for_apply(authorization, stage)

    assert str(caught.value) == "candidate Stage integrity re-verification failed"
    assert caught.value.__suppress_context__ is True


def test_binding_drift_during_integrity_verification_fails_closed(tmp_path, monkeypatch):
    prepared = _prepared()
    authorization = _authorization(prepared)
    stage = _stage(tmp_path, prepared)

    def verifier(value):
        object.__setattr__(value, "commit_sha", "e" * 40)

    monkeypatch.setattr("ha_syncapp.stage_prewrite_reproof.verify_candidate_stage", verifier)

    with pytest.raises(StagePrewriteReproofError, match="changed during verification"):
        reprove_stage_for_apply(authorization, stage)


def test_privileged_prewrite_evidence_cannot_be_constructed_directly():
    with pytest.raises(StagePrewriteReproofError, match="trusted producer"):
        StagePrewriteEvidence(
            deployment_id="deployment",
            target="owner/private-repo",
            repository_id=12345,
            candidate_sha="b" * 40,
            stage_manifest_sha256="c" * 64,
        )


def test_wrong_input_types_fail_closed_without_nested_details(tmp_path):
    prepared = _prepared()
    authorization = _authorization(prepared)

    with pytest.raises(StagePrewriteReproofError) as caught:
        reprove_stage_for_apply(authorization, object())  # type: ignore[arg-type]

    assert caught.value.__suppress_context__ is True
    assert "object" not in str(caught.value)
