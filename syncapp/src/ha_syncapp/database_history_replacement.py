from __future__ import annotations

from dataclasses import dataclass

from ha_syncapp.database_history_evidence import TrustedDatabaseHistoryEvidence
from ha_syncapp.database_history_prewrite import TrustedDatabaseHistoryPrewrite
from ha_syncapp.database_retention import (
    DATABASE_BRANCH,
    DatabaseRetentionError,
    DatabaseSnapshotEvidence,
    plan_database_retention,
)


class DatabaseHistoryReplacementAuthorizationError(ValueError):
    """Raised when database history replacement cannot be authorized safely."""


@dataclass(frozen=True, slots=True, init=False)
class DatabaseHistoryReplacementAuthorization:
    """Side-effect-free authorization for one exact database retention replacement."""

    target: str
    repository_id: int
    branch: str
    expected_head_sha: str
    retained_shas: tuple[str, ...]
    pruned_shas: tuple[str, ...]

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "DatabaseHistoryReplacementAuthorization must be produced by "
            "authorize_database_history_replacement()"
        )

    @property
    def requires_replacement(self) -> bool:
        return bool(self.pruned_shas)


def authorize_database_history_replacement(
    *,
    evidence: TrustedDatabaseHistoryEvidence,
    prewrite: TrustedDatabaseHistoryPrewrite,
) -> DatabaseHistoryReplacementAuthorization:
    """Bind a validated Recorder retention plan to a fresh exact-head proof."""

    if type(evidence) is not TrustedDatabaseHistoryEvidence:
        raise DatabaseHistoryReplacementAuthorizationError(
            "trusted database history evidence is invalid"
        )
    if type(prewrite) is not TrustedDatabaseHistoryPrewrite:
        raise DatabaseHistoryReplacementAuthorizationError(
            "trusted database prewrite proof is invalid"
        )

    plan = evidence.plan
    if (
        evidence.branch != DATABASE_BRANCH
        or plan.branch != DATABASE_BRANCH
        or prewrite.branch != DATABASE_BRANCH
    ):
        raise DatabaseHistoryReplacementAuthorizationError(
            "history replacement authorization is restricted to the database branch"
        )
    if (
        not isinstance(evidence.target, str)
        or not evidence.target.strip()
        or type(evidence.repository_id) is not int
        or evidence.repository_id <= 0
    ):
        raise DatabaseHistoryReplacementAuthorizationError(
            "trusted database repository identity is invalid"
        )
    if (
        evidence.target.casefold() != prewrite.target.casefold()
        or evidence.repository_id != prewrite.repository_id
        or evidence.expected_head_sha != prewrite.expected_head_sha
    ):
        raise DatabaseHistoryReplacementAuthorizationError(
            "trusted database prewrite proof does not match retention evidence"
        )

    retained = plan.retained_identities
    pruned = plan.prunable_identities
    if not retained or retained[0] != evidence.expected_head_sha:
        raise DatabaseHistoryReplacementAuthorizationError(
            "trusted database retained history is invalid"
        )
    if len(set(retained)) != len(retained) or len(set(pruned)) != len(pruned):
        raise DatabaseHistoryReplacementAuthorizationError(
            "trusted database retention authorization contains duplicate commits"
        )
    if set(retained).intersection(pruned):
        raise DatabaseHistoryReplacementAuthorizationError(
            "trusted database retained and pruned history overlap"
        )
    history_shas = tuple(record.sha for record in evidence.records)
    if retained + pruned != history_shas:
        raise DatabaseHistoryReplacementAuthorizationError(
            "trusted database retention authorization does not match history evidence"
        )

    snapshots = tuple(
        DatabaseSnapshotEvidence(identity=record.sha, created_at=record.committed_at)
        for record in evidence.records
    )
    try:
        validated_plan = plan_database_retention(
            branch=DATABASE_BRANCH,
            snapshots=snapshots,
            reference_time=plan.reference_time,
            retention_days=plan.retention_days,
        )
    except DatabaseRetentionError as error:
        raise DatabaseHistoryReplacementAuthorizationError(str(error)) from None
    if validated_plan != plan:
        raise DatabaseHistoryReplacementAuthorizationError(
            "trusted database retention plan is inconsistent"
        )

    return _database_history_replacement_authorization(
        target=evidence.target,
        repository_id=evidence.repository_id,
        expected_head_sha=evidence.expected_head_sha,
        retained_shas=retained,
        pruned_shas=pruned,
    )


def _database_history_replacement_authorization(
    *,
    target: str,
    repository_id: int,
    expected_head_sha: str,
    retained_shas: tuple[str, ...],
    pruned_shas: tuple[str, ...],
) -> DatabaseHistoryReplacementAuthorization:
    authorization = object.__new__(DatabaseHistoryReplacementAuthorization)
    object.__setattr__(authorization, "target", target)
    object.__setattr__(authorization, "repository_id", repository_id)
    object.__setattr__(authorization, "branch", DATABASE_BRANCH)
    object.__setattr__(authorization, "expected_head_sha", expected_head_sha)
    object.__setattr__(authorization, "retained_shas", retained_shas)
    object.__setattr__(authorization, "pruned_shas", pruned_shas)
    return authorization
