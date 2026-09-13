from __future__ import annotations

from dataclasses import dataclass

from ha_syncapp.database_history_evidence import TrustedDatabaseHistoryEvidence
from ha_syncapp.database_retention import DATABASE_BRANCH
from ha_syncapp.github_repo import (
    BranchHead,
    RepositoryVerificationError,
    fetch_trusted_branch_head,
)


class DatabaseHistoryPrewriteError(RuntimeError):
    """Database history replacement cannot safely proceed from supplied evidence."""


@dataclass(frozen=True, slots=True, init=False)
class TrustedDatabaseHistoryPrewrite:
    """Fresh identity/head proof produced only by the database prewrite verifier."""

    target: str
    repository_id: int
    branch: str
    expected_head_sha: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "TrustedDatabaseHistoryPrewrite must be produced by reprove_database_history_prewrite()"
        )


def reprove_database_history_prewrite(
    *,
    evidence: TrustedDatabaseHistoryEvidence,
    token: str,
) -> TrustedDatabaseHistoryPrewrite:
    """Re-prove Repo B identity and the exact database head without mutating Git."""

    _validate_evidence_boundary(evidence)
    try:
        current = fetch_trusted_branch_head(
            evidence.target,
            token,
            expected_id=evidence.repository_id,
            branch=DATABASE_BRANCH,
        )
    except RepositoryVerificationError:
        raise DatabaseHistoryPrewriteError(
            "trusted database repository verification failed"
        ) from None

    _require_unchanged_head(evidence, current)
    return _trusted_database_history_prewrite(current)


def _trusted_database_history_prewrite(current: BranchHead) -> TrustedDatabaseHistoryPrewrite:
    proof = object.__new__(TrustedDatabaseHistoryPrewrite)
    object.__setattr__(proof, "target", current.target)
    object.__setattr__(proof, "repository_id", current.repository_id)
    object.__setattr__(proof, "branch", current.branch)
    object.__setattr__(proof, "expected_head_sha", current.commit_sha)
    return proof


def _validate_evidence_boundary(evidence: TrustedDatabaseHistoryEvidence) -> None:
    if type(evidence) is not TrustedDatabaseHistoryEvidence:
        raise DatabaseHistoryPrewriteError("trusted database history evidence is invalid")
    if evidence.branch != DATABASE_BRANCH or evidence.plan.branch != DATABASE_BRANCH:
        raise DatabaseHistoryPrewriteError(
            "history replacement is restricted to the database branch"
        )
    if (
        not isinstance(evidence.target, str)
        or not evidence.target.strip()
        or type(evidence.repository_id) is not int
        or evidence.repository_id <= 0
    ):
        raise DatabaseHistoryPrewriteError("trusted database repository identity is invalid")
    if not evidence.records or evidence.records[0].sha != evidence.expected_head_sha:
        raise DatabaseHistoryPrewriteError("trusted database history evidence head is inconsistent")


def _require_unchanged_head(
    evidence: TrustedDatabaseHistoryEvidence,
    current: BranchHead,
) -> None:
    if (
        current.target.casefold() != evidence.target.casefold()
        or current.repository_id != evidence.repository_id
        or current.branch != evidence.branch
        or current.commit_sha != evidence.expected_head_sha
    ):
        raise DatabaseHistoryPrewriteError("trusted database history changed before replacement")
