"""Ephemeral authorization evidence for one exact future candidate Apply."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from .candidate_backup import CandidateBackupEvidence
from .preapply_freshness import PreApplyFreshnessEvidence
from .prepared_deployment import PreparedDeployment, PreparedDeploymentError


class ApplyAuthorizationError(RuntimeError):
    """The exact pre-Apply evidence chain could not authorize live mutation."""


_AUTHORIZATION_PRODUCER = object()


@dataclass(frozen=True, slots=True, init=False)
class ApplyAuthorization:
    """Immutable permit for one exact candidate; construction is producer-confined."""

    deployment_id: str
    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    backup_slug: str
    stage_manifest_sha256: str
    runtime_sha256: str
    risk_level: str
    core_version: str

    def __init__(
        self,
        *,
        deployment_id: str,
        target: str,
        repository_id: int,
        baseline_sha: str,
        candidate_sha: str,
        backup_slug: str,
        stage_manifest_sha256: str,
        runtime_sha256: str,
        risk_level: str,
        core_version: str,
        _producer: object | None = None,
    ) -> None:
        if _producer is not _AUTHORIZATION_PRODUCER:
            _reject("Apply authorization must be created by the trusted producer")
        values = (
            ("deployment_id", deployment_id),
            ("target", target),
            ("repository_id", repository_id),
            ("baseline_sha", baseline_sha),
            ("candidate_sha", candidate_sha),
            ("backup_slug", backup_slug),
            ("stage_manifest_sha256", stage_manifest_sha256),
            ("runtime_sha256", runtime_sha256),
            ("risk_level", risk_level),
            ("core_version", core_version),
        )
        for name, value in values:
            object.__setattr__(self, name, value)

    @classmethod
    def _from_verified_chain(
        cls,
        prepared: PreparedDeployment,
        evidence: CandidateBackupEvidence,
    ) -> ApplyAuthorization:
        return cls(
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
            _producer=_AUTHORIZATION_PRODUCER,
        )


def authorize_candidate_apply(
    prepared: PreparedDeployment,
    backup: CandidateBackupEvidence,
    freshness: PreApplyFreshnessEvidence,
) -> ApplyAuthorization:
    """Bind the complete freshly re-proven chain without performing any mutation."""
    if type(prepared) is not PreparedDeployment:
        _reject("prepared deployment evidence is invalid")
    if type(backup) is not CandidateBackupEvidence:
        _reject("prepared backup evidence is invalid")
    if type(freshness) is not PreApplyFreshnessEvidence:
        _reject("pre-Apply freshness evidence is invalid")

    try:
        prepared.validate()
    except PreparedDeploymentError:
        _reject("prepared deployment evidence is invalid")

    if prepared.evidence != backup:
        _reject("prepared backup binding does not match")

    expected = (
        backup.target,
        backup.repository_id,
        backup.baseline_sha,
        backup.candidate_sha,
        backup.backup_slug,
        backup.stage_manifest_sha256,
        backup.runtime_sha256,
        backup.risk_level,
        backup.core_version,
    )
    observed = (
        freshness.target,
        freshness.repository_id,
        freshness.baseline_sha,
        freshness.candidate_sha,
        freshness.backup_slug,
        freshness.stage_manifest_sha256,
        freshness.runtime_sha256,
        freshness.risk_level,
        freshness.core_version,
    )
    if observed != expected:
        _reject("pre-Apply freshness binding does not match")

    return ApplyAuthorization._from_verified_chain(prepared, backup)


def _reject(message: str) -> NoReturn:
    raise ApplyAuthorizationError(message) from None
