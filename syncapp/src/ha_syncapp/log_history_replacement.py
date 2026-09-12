from __future__ import annotations

from dataclasses import dataclass

from ha_syncapp.log_history_evidence import TrustedLogHistoryEvidence
from ha_syncapp.log_history_prewrite import TrustedLogHistoryPrewrite
from ha_syncapp.log_history_retention import LOG_HISTORY_BRANCH


class LogHistoryReplacementAuthorizationError(ValueError):
    """Raised when logs history replacement cannot be authorized safely."""


@dataclass(frozen=True, slots=True)
class LogHistoryReplacementAuthorization:
    """Side-effect-free authorization for one exact logs retention replacement."""

    target: str
    repository_id: int
    branch: str
    expected_head_sha: str
    retained_shas: tuple[str, ...]
    pruned_shas: tuple[str, ...]

    @property
    def requires_replacement(self) -> bool:
        return bool(self.pruned_shas)


def authorize_log_history_replacement(
    *,
    evidence: TrustedLogHistoryEvidence,
    prewrite: TrustedLogHistoryPrewrite,
) -> LogHistoryReplacementAuthorization:
    """Bind one validated retention plan to its immediately refreshed logs proof."""

    if type(evidence) is not TrustedLogHistoryEvidence:
        raise LogHistoryReplacementAuthorizationError("trusted logs history evidence is invalid")
    if type(prewrite) is not TrustedLogHistoryPrewrite:
        raise LogHistoryReplacementAuthorizationError("trusted logs prewrite proof is invalid")

    plan = evidence.plan
    if (
        evidence.branch != LOG_HISTORY_BRANCH
        or plan.branch != LOG_HISTORY_BRANCH
        or prewrite.branch != LOG_HISTORY_BRANCH
    ):
        raise LogHistoryReplacementAuthorizationError(
            "history replacement authorization is restricted to the logs branch"
        )

    if (
        evidence.target.casefold() != prewrite.target.casefold()
        or evidence.repository_id != prewrite.repository_id
        or evidence.expected_head_sha != prewrite.expected_head_sha
    ):
        raise LogHistoryReplacementAuthorizationError(
            "trusted logs prewrite proof does not match retention evidence"
        )
    if plan.expected_head_sha != evidence.expected_head_sha:
        raise LogHistoryReplacementAuthorizationError("trusted logs retention head is inconsistent")

    retained = plan.retained_shas
    pruned = plan.pruned_shas
    if not retained or retained[0] != evidence.expected_head_sha:
        raise LogHistoryReplacementAuthorizationError("trusted logs retained history is invalid")
    if len(set(retained)) != len(retained) or len(set(pruned)) != len(pruned):
        raise LogHistoryReplacementAuthorizationError(
            "trusted logs retention authorization contains duplicate commits"
        )
    if set(retained).intersection(pruned):
        raise LogHistoryReplacementAuthorizationError(
            "trusted logs retained and pruned history overlap"
        )

    evidence_shas = tuple(commit.sha for commit in evidence.commits)
    if retained + pruned != evidence_shas:
        raise LogHistoryReplacementAuthorizationError(
            "trusted logs retention authorization does not match history evidence"
        )

    return LogHistoryReplacementAuthorization(
        target=evidence.target,
        repository_id=evidence.repository_id,
        branch=LOG_HISTORY_BRANCH,
        expected_head_sha=evidence.expected_head_sha,
        retained_shas=retained,
        pruned_shas=pruned,
    )
