from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from ha_syncapp.apply_authorization import (
    ApplyAuthorization,
    ApplyAuthorizationError,
    authorize_candidate_apply,
)
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.preapply_freshness import (
    PreApplyFreshnessEvidence,
    reprove_preapply_repo_heads,
)
from ha_syncapp.prepared_deployment import PreparedDeployment


def _evidence() -> CandidateBackupEvidence:
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
    return PreparedDeployment(str(uuid4()), _evidence(), datetime.now(UTC))


def _freshness(prepared: PreparedDeployment) -> PreApplyFreshnessEvidence:
    evidence = prepared.evidence

    def fetcher(target, token, *, expected_id, branch="main"):
        sha = evidence.baseline_sha if branch == "main" else evidence.candidate_sha
        return BranchHead(target, expected_id, branch, sha)

    return reprove_preapply_repo_heads(
        prepared,
        evidence,
        token="secret-token",
        head_fetcher=fetcher,
    )


def test_exact_reproven_chain_produces_immutable_authorization():
    prepared = _prepared()

    result = authorize_candidate_apply(prepared, prepared.evidence, _freshness(prepared))

    assert result.deployment_id == prepared.deployment_id
    assert result.target == prepared.evidence.target
    assert result.repository_id == prepared.evidence.repository_id
    assert result.baseline_sha == prepared.evidence.baseline_sha
    assert result.candidate_sha == prepared.evidence.candidate_sha
    assert result.backup_slug == prepared.evidence.backup_slug
    assert result.stage_manifest_sha256 == prepared.evidence.stage_manifest_sha256
    assert result.runtime_sha256 == prepared.evidence.runtime_sha256
    assert result.risk_level == prepared.evidence.risk_level
    assert result.core_version == prepared.evidence.core_version

    with pytest.raises(FrozenInstanceError):
        result.candidate_sha = "e" * 40  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target", "other/private-repo"),
        ("repository_id", 54321),
        ("baseline_sha", "e" * 40),
        ("candidate_sha", "f" * 40),
        ("backup_slug", "backup_other"),
        ("stage_manifest_sha256", "1" * 64),
        ("runtime_sha256", "2" * 64),
        ("risk_level", "medium"),
        ("core_version", "2026.9.2"),
    ],
)
def test_any_freshness_binding_drift_fails_closed(field, value):
    prepared = _prepared()
    freshness = _freshness(prepared)
    object.__setattr__(freshness, field, value)

    with pytest.raises(ApplyAuthorizationError, match="freshness"):
        authorize_candidate_apply(prepared, prepared.evidence, freshness)


def test_backup_must_be_exact_prepared_binding():
    prepared = _prepared()
    wrong_backup = replace(prepared.evidence, backup_slug="backup_other")

    with pytest.raises(ApplyAuthorizationError, match="backup"):
        authorize_candidate_apply(prepared, wrong_backup, _freshness(prepared))


def test_wrong_input_types_fail_closed_without_nested_details():
    prepared = _prepared()

    with pytest.raises(ApplyAuthorizationError) as caught:
        authorize_candidate_apply(  # type: ignore[arg-type]
            prepared, object(), _freshness(prepared)
        )

    assert caught.value.__suppress_context__ is True
    assert "object" not in str(caught.value)


def test_privileged_authorization_cannot_be_constructed_directly():
    prepared = _prepared()
    evidence = prepared.evidence

    with pytest.raises(ApplyAuthorizationError, match="producer"):
        ApplyAuthorization(
            deployment_id=prepared.deployment_id,
            target=evidence.target,
            repository_id=evidence.repository_id,
            baseline_sha=evidence.baseline_sha,
            candidate_sha=evidence.candidate_sha,
            backup_slug=evidence.backup_slug,
            stage_manifest_sha256=evidence.stage_manifest_sha256,
            runtime_sha256=evidence.runtime_sha256,
            risk_level=evidence.risk_level,
            core_version=evidence.core_version,
        )
