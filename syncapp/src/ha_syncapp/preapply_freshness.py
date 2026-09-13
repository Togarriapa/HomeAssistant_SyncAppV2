"""Read-only Repo B freshness evidence for a future candidate Apply gate."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from .candidate_backup import CandidateBackupEvidence
from .github_repo import (
    BranchHead,
    RepositoryVerificationError,
    fetch_trusted_branch_head,
)
from .prepared_deployment import PreparedDeployment, PreparedDeploymentError


class PreApplyFreshnessError(RuntimeError):
    """Fresh trusted Repo B heads could not be proven for the prepared candidate."""


@dataclass(frozen=True, slots=True)
class PreApplyFreshnessEvidence:
    """Immutable proof that Repo B still matches one prepared candidate and backup."""

    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    backup_slug: str
    stage_manifest_sha256: str
    runtime_sha256: str
    risk_level: str
    core_version: str


TrustedHeadFetcher = Callable[..., BranchHead]


def reprove_preapply_repo_heads(
    prepared: PreparedDeployment,
    backup: CandidateBackupEvidence,
    *,
    token: str | None,
    head_fetcher: TrustedHeadFetcher = fetch_trusted_branch_head,
) -> PreApplyFreshnessEvidence:
    """Re-prove exact private ``main`` and ``candidate`` heads without mutation.

    Persisted preparation and backup evidence are prerequisites only.  This function
    grants no Apply authority and performs no Home Assistant, Supervisor, or Git
    mutation.
    """
    try:
        # Keep a detached immutable value snapshot. ``CandidateBackupEvidence`` is
        # frozen for normal callers, but Python reflection can still mutate an
        # aliased instance via ``object.__setattr__`` while external I/O is in
        # progress. Comparing against an alias would make that drift invisible.
        expected = replace(_validate_binding(prepared, backup))
        credential = _validate_token(token)

        main = _fetch_head(
            head_fetcher,
            expected.target,
            credential,
            expected.repository_id,
            "main",
        )
        _require_binding_unchanged(prepared, backup, expected)
        if main.commit_sha != expected.baseline_sha:
            raise PreApplyFreshnessError("trusted main head is stale")

        candidate = _fetch_head(
            head_fetcher,
            expected.target,
            credential,
            expected.repository_id,
            "candidate",
        )
        _require_binding_unchanged(prepared, backup, expected)
        if candidate.commit_sha != expected.candidate_sha:
            raise PreApplyFreshnessError("trusted candidate head is stale")

        # Re-prove the complete semantic binding after all external I/O as a final
        # defense before freshness evidence is returned.
        _validate_binding(prepared, backup)

        return PreApplyFreshnessEvidence(
            target=expected.target,
            repository_id=expected.repository_id,
            baseline_sha=expected.baseline_sha,
            candidate_sha=expected.candidate_sha,
            backup_slug=expected.backup_slug,
            stage_manifest_sha256=expected.stage_manifest_sha256,
            runtime_sha256=expected.runtime_sha256,
            risk_level=expected.risk_level,
            core_version=expected.core_version,
        )
    except PreApplyFreshnessError:
        raise
    except RepositoryVerificationError:
        raise PreApplyFreshnessError("trusted repository freshness could not be verified") from None
    except Exception:
        raise PreApplyFreshnessError(
            "pre-Apply repository freshness could not be established"
        ) from None


def _validate_binding(
    prepared: PreparedDeployment,
    backup: CandidateBackupEvidence,
) -> CandidateBackupEvidence:
    if type(prepared) is not PreparedDeployment or type(backup) is not CandidateBackupEvidence:
        raise PreApplyFreshnessError("prepared backup evidence is invalid")
    try:
        prepared.validate()
    except PreparedDeploymentError:
        raise PreApplyFreshnessError("prepared deployment evidence is invalid") from None
    if prepared.evidence != backup:
        raise PreApplyFreshnessError("prepared backup binding does not match")
    return backup


def _require_binding_unchanged(
    prepared: PreparedDeployment,
    backup: CandidateBackupEvidence,
    expected: CandidateBackupEvidence,
) -> None:
    if prepared.evidence != expected or backup != expected:
        raise PreApplyFreshnessError("prepared deployment evidence changed during verification")


def _validate_token(token: str | None) -> str:
    if not isinstance(token, str) or not token or token != token.strip():
        raise PreApplyFreshnessError("GitHub credential is unavailable")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in token):
        raise PreApplyFreshnessError("GitHub credential is invalid")
    return token


def _fetch_head(
    fetcher: TrustedHeadFetcher,
    target: str,
    token: str,
    repository_id: int,
    branch: str,
) -> BranchHead:
    try:
        head = fetcher(target, token, expected_id=repository_id, branch=branch)
    except RepositoryVerificationError:
        raise
    except Exception:
        raise PreApplyFreshnessError("trusted repository freshness check failed") from None
    if type(head) is not BranchHead:
        raise PreApplyFreshnessError("trusted repository freshness evidence is invalid")
    if (
        head.target.casefold() != target.casefold()
        or head.repository_id != repository_id
        or head.branch != branch
    ):
        raise PreApplyFreshnessError("trusted repository freshness evidence is invalid")
    return head
