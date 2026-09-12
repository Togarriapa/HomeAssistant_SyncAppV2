from __future__ import annotations

from dataclasses import dataclass

from ha_syncapp.github_repo import (
    BranchHead,
    RepositoryVerificationError,
    fetch_trusted_branch_head,
)
from ha_syncapp.log_history_evidence import TrustedLogHistoryEvidence
from ha_syncapp.log_history_retention import LOG_HISTORY_BRANCH


class LogHistoryPrewriteError(RuntimeError):
    """Logs history replacement cannot safely proceed from the supplied evidence."""


@dataclass(frozen=True, slots=True)
class TrustedLogHistoryPrewrite:
    """Fresh identity/head proof for a future logs-only history replacement."""

    target: str
    repository_id: int
    branch: str
    expected_head_sha: str


def reprove_log_history_prewrite(
    *,
    evidence: TrustedLogHistoryEvidence,
    token: str,
) -> TrustedLogHistoryPrewrite:
    """Re-prove Repo B identity and the exact logs head without mutating Git."""

    _validate_evidence_boundary(evidence)
    try:
        current = fetch_trusted_branch_head(
            evidence.target,
            token,
            expected_id=evidence.repository_id,
            branch=LOG_HISTORY_BRANCH,
        )
    except RepositoryVerificationError:
        raise LogHistoryPrewriteError("trusted logs repository verification failed") from None

    _require_unchanged_head(evidence, current)
    return TrustedLogHistoryPrewrite(
        target=current.target,
        repository_id=current.repository_id,
        branch=current.branch,
        expected_head_sha=current.commit_sha,
    )


def _validate_evidence_boundary(evidence: TrustedLogHistoryEvidence) -> None:
    if not isinstance(evidence, TrustedLogHistoryEvidence):
        raise LogHistoryPrewriteError("trusted logs history evidence is invalid")
    if evidence.branch != LOG_HISTORY_BRANCH or evidence.plan.branch != LOG_HISTORY_BRANCH:
        raise LogHistoryPrewriteError("history replacement is restricted to the logs branch")
    if (
        not isinstance(evidence.target, str)
        or not evidence.target.strip()
        or type(evidence.repository_id) is not int
        or evidence.repository_id <= 0
    ):
        raise LogHistoryPrewriteError("trusted logs repository identity is invalid")
    if evidence.plan.expected_head_sha != evidence.expected_head_sha:
        raise LogHistoryPrewriteError("trusted logs history evidence head is inconsistent")
    if not evidence.commits or evidence.commits[0].sha != evidence.expected_head_sha:
        raise LogHistoryPrewriteError("trusted logs history evidence head is inconsistent")


def _require_unchanged_head(
    evidence: TrustedLogHistoryEvidence,
    current: BranchHead,
) -> None:
    if (
        current.target.casefold() != evidence.target.casefold()
        or current.repository_id != evidence.repository_id
        or current.branch != evidence.branch
        or current.commit_sha != evidence.expected_head_sha
    ):
        raise LogHistoryPrewriteError("trusted logs history changed before replacement")
