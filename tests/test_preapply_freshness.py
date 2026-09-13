from datetime import UTC, datetime
from uuid import uuid4

import pytest
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.github_repo import BranchHead, RepositoryVerificationError
from ha_syncapp.preapply_freshness import (
    PreApplyFreshnessError,
    reprove_preapply_repo_heads,
)
from ha_syncapp.prepared_deployment import PreparedDeployment


def _prepared():
    evidence = CandidateBackupEvidence(
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
    return PreparedDeployment(str(uuid4()), evidence, datetime.now(UTC))


def test_exact_main_and_candidate_heads_produce_immutable_freshness_evidence():
    prepared = _prepared()
    calls = []

    def fetcher(target, token, *, expected_id, branch="main"):
        calls.append((target, token, expected_id, branch))
        sha = (
            prepared.evidence.baseline_sha if branch == "main" else prepared.evidence.candidate_sha
        )
        return BranchHead(target, expected_id, branch, sha)

    result = reprove_preapply_repo_heads(
        prepared, prepared.evidence, token="secret-token", head_fetcher=fetcher
    )

    assert result.target == prepared.evidence.target
    assert result.repository_id == prepared.evidence.repository_id
    assert result.baseline_sha == prepared.evidence.baseline_sha
    assert result.candidate_sha == prepared.evidence.candidate_sha
    assert result.backup_slug == prepared.evidence.backup_slug
    assert result.stage_manifest_sha256 == prepared.evidence.stage_manifest_sha256
    assert result.runtime_sha256 == prepared.evidence.runtime_sha256
    assert result.risk_level == prepared.evidence.risk_level
    assert result.core_version == prepared.evidence.core_version
    assert [call[3] for call in calls] == ["main", "candidate"]


@pytest.mark.parametrize("moved_branch", ["main", "candidate"])
def test_moved_trusted_head_fails_closed(moved_branch):
    prepared = _prepared()

    def fetcher(target, token, *, expected_id, branch="main"):
        expected = (
            prepared.evidence.baseline_sha if branch == "main" else prepared.evidence.candidate_sha
        )
        sha = "e" * 40 if branch == moved_branch else expected
        return BranchHead(target, expected_id, branch, sha)

    with pytest.raises(PreApplyFreshnessError, match="stale"):
        reprove_preapply_repo_heads(
            prepared, prepared.evidence, token="secret-token", head_fetcher=fetcher
        )


def test_backup_binding_mismatch_fails_before_github_io():
    prepared = _prepared()
    other = CandidateBackupEvidence(**{**prepared.evidence.__dict__, "backup_slug": "other"})

    with pytest.raises(PreApplyFreshnessError, match="backup"):
        reprove_preapply_repo_heads(
            prepared,
            other,
            token="secret-token",
            head_fetcher=lambda *_args, **_kwargs: pytest.fail("must not call GitHub"),
        )


def test_repository_verification_failure_is_sanitized():
    prepared = _prepared()

    def fetcher(*_args, **_kwargs):
        raise RepositoryVerificationError("secret-token private-repo-canary")

    with pytest.raises(PreApplyFreshnessError) as caught:
        reprove_preapply_repo_heads(
            prepared, prepared.evidence, token="secret-token", head_fetcher=fetcher
        )

    assert "secret-token" not in str(caught.value)
    assert "private-repo-canary" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_prepared_evidence_drift_during_github_io_is_rejected():
    prepared = _prepared()
    original = prepared.evidence

    def fetcher(target, token, *, expected_id, branch="main"):
        if branch == "candidate":
            object.__setattr__(
                prepared,
                "evidence",
                CandidateBackupEvidence(
                    target=original.target,
                    repository_id=original.repository_id,
                    baseline_sha=original.baseline_sha,
                    candidate_sha="f" * 40,
                    stage_manifest_sha256=original.stage_manifest_sha256,
                    runtime_sha256=original.runtime_sha256,
                    risk_level=original.risk_level,
                    core_version=original.core_version,
                    backup_slug=original.backup_slug,
                ),
            )
        sha = original.baseline_sha if branch == "main" else original.candidate_sha
        return BranchHead(target, expected_id, branch, sha)

    with pytest.raises(PreApplyFreshnessError, match="changed"):
        reprove_preapply_repo_heads(prepared, original, token="secret-token", head_fetcher=fetcher)
