from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from ha_syncapp.github_repo import BranchHead
from ha_syncapp.log_history_retention import (
    LOG_HISTORY_BRANCH,
    MAX_HISTORY_COMMITS,
    LogHistoryCommit,
    LogHistoryRetentionError,
    LogHistoryRetentionPlan,
    plan_log_history_retention,
)

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class LogHistoryEvidenceError(ValueError):
    """Raised when trusted logs history evidence is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class LogHistoryRecord:
    sha: str
    committed_at: datetime
    parent_shas: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrustedLogHistoryEvidence:
    target: str
    repository_id: int
    branch: str
    expected_head_sha: str
    commits: tuple[LogHistoryCommit, ...]
    plan: LogHistoryRetentionPlan


def validate_trusted_log_history_evidence(
    *,
    branch_head: BranchHead,
    records: Iterable[LogHistoryRecord],
    reference_time: datetime,
) -> TrustedLogHistoryEvidence:
    """Bind complete linear logs history to one already verified Repo B head."""

    if branch_head.branch != LOG_HISTORY_BRANCH:
        raise LogHistoryEvidenceError("history evidence is restricted to the logs branch")
    if (
        not isinstance(branch_head.target, str)
        or not branch_head.target.strip()
        or type(branch_head.repository_id) is not int
        or branch_head.repository_id <= 0
    ):
        raise LogHistoryEvidenceError("trusted repository identity is invalid")
    if _SHA_PATTERN.fullmatch(branch_head.commit_sha) is None:
        raise LogHistoryEvidenceError("trusted logs head is invalid")

    history = _bounded_records(records)
    if not history:
        raise LogHistoryEvidenceError("trusted logs history must not be empty")
    if history[0].sha != branch_head.commit_sha:
        raise LogHistoryEvidenceError("trusted logs history does not match the verified head")

    for index, record in enumerate(history):
        if _SHA_PATTERN.fullmatch(record.sha) is None:
            raise LogHistoryEvidenceError("logs history contains an invalid commit SHA")
        if any(_SHA_PATTERN.fullmatch(parent) is None for parent in record.parent_shas):
            raise LogHistoryEvidenceError("logs history contains an invalid parent SHA")
        if len(record.parent_shas) > 1:
            raise LogHistoryEvidenceError("logs history must be linear")

        is_root = index == len(history) - 1
        if is_root:
            if record.parent_shas:
                raise LogHistoryEvidenceError("logs history evidence must be complete to the root")
            continue

        if record.parent_shas != (history[index + 1].sha,):
            raise LogHistoryEvidenceError("logs history parent chain is inconsistent")

    commits = tuple(
        LogHistoryCommit(sha=record.sha, committed_at=record.committed_at) for record in history
    )
    try:
        plan = plan_log_history_retention(
            branch=LOG_HISTORY_BRANCH,
            commits=commits,
            reference_time=reference_time,
        )
    except LogHistoryRetentionError as error:
        raise LogHistoryEvidenceError(str(error)) from None

    return TrustedLogHistoryEvidence(
        target=branch_head.target,
        repository_id=branch_head.repository_id,
        branch=LOG_HISTORY_BRANCH,
        expected_head_sha=branch_head.commit_sha,
        commits=commits,
        plan=plan,
    )


def _bounded_records(records: Iterable[LogHistoryRecord]) -> tuple[LogHistoryRecord, ...]:
    history: list[LogHistoryRecord] = []
    for record in records:
        if len(history) >= MAX_HISTORY_COMMITS:
            raise LogHistoryEvidenceError("logs history exceeds the evidence limit")
        if not isinstance(record, LogHistoryRecord):
            raise LogHistoryEvidenceError("logs history contains invalid evidence")
        history.append(record)
    return tuple(history)
