"""Ephemeral authorization evidence for one exact future candidate Apply."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from .candidate_backup import CandidateBackupEvidence
from .preapply_freshness import PreApplyFreshnessEvidence
from .prepared_deployment import PreparedDeployment, PreparedDeploymentError


class ApplyAuthorizationError(RuntimeError):
    """The exact pre-Apply evidence chain could not authorize live mutation."""


@dataclass(frozen=True, slots=True, init=False)
class ApplyAuthorization:
    """Immutable permit whose construction re-validates the full trusted chain."""

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
        prepared: PreparedDeployment | None = None,
        backup: CandidateBackupEvidence | None = None,
        freshness: PreApplyFreshnessEvidence | None = None,
        **forbidden: object,
    ) -> None:
        if forbidden:
            _reject("Apply authorization must be created from the verified evidence chain")
        validated_prepared, validated_backup, _ = _validate_chain(
            prepared, backup, freshness
        )
        values = (
            ("deployment_id", validated_prepared.deployment_id),
            ("target", validated_backup.target),
            ("repository_id", validated_backup.repository_id),
            ("baseline_sha", validated_backup.baseline_sha),
            ("candidate_sha", validated_backup.candidate_sha),
            ("backup_slug", validated_backup.backup_slug),
            ("stage_manifest_sha256", validated_backup.stage_manifest_sha256),
            ("runtime_sha256", validated_backup.runtime_sha256),
            ("risk_level", validated_backup.risk_level),
            ("core_version", validated_backup.core_version),
        )
        for name, value in values:
            object.__setattr__(self, name, value)


def authorize_candidate_apply(
    prepared: PreparedDeployment,
    backup: CandidateBackupEvidence,
    freshness: PreApplyFreshnessEvidence,
) -> ApplyAuthorization:
    """Bind the complete freshly re-proven chain without performing any mutation."""
    return ApplyAuthorization(prepared, backup, freshness)


def _validate_chain(
    prepared: PreparedDeployment | None,
    backup: CandidateBackupEvidence | None,
    freshness: PreApplyFreshnessEvidence | None,
) -> tuple[PreparedDeployment, CandidateBackupEvidence, PreApplyFreshnessEvidence]:
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
    return prepared, backup, freshness


def _reject(message: str) -> NoReturn:
    raise ApplyAuthorizationError(message) from None
