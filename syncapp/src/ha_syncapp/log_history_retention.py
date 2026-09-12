from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


LOG_HISTORY_BRANCH = "logs"
LOG_RETENTION_DAYS = 30
MAX_HISTORY_COMMITS = 4096
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class LogHistoryRetentionError(ValueError):
    """Raised when log-history evidence cannot be retained safely."""


@dataclass(frozen=True, slots=True)
class LogHistoryCommit:
    sha: str
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class LogHistoryRetentionPlan:
    branch: str
    expected_head_sha: str
    reference_time: datetime
    cutoff: datetime
    retained_shas: tuple[str, ...]
    pruned_shas: tuple[str, ...]


def plan_log_history_retention(
    *,
    branch: str,
    commits: Iterable[LogHistoryCommit],
    reference_time: datetime,
) -> LogHistoryRetentionPlan:
    """Classify generated ``logs`` history without mutating Git or remote refs."""

    if branch != LOG_HISTORY_BRANCH:
        raise LogHistoryRetentionError("history retention is restricted to the logs branch")
    _require_utc(reference_time, field="reference_time")

    history = _bounded_history(commits)
    if not history:
        raise LogHistoryRetentionError("logs history must contain a current head")

    seen: set[str] = set()
    previous_time: datetime | None = None
    for commit in history:
        if _SHA_PATTERN.fullmatch(commit.sha) is None:
            raise LogHistoryRetentionError("logs history contains an invalid commit SHA")
        if commit.sha in seen:
            raise LogHistoryRetentionError("logs history contains a duplicate commit SHA")
        seen.add(commit.sha)

        _require_utc(commit.committed_at, field="committed_at")
        if commit.committed_at > reference_time:
            raise LogHistoryRetentionError("logs history contains a future commit timestamp")
        if previous_time is not None and commit.committed_at > previous_time:
            raise LogHistoryRetentionError("logs history must be ordered newest-first")
        previous_time = commit.committed_at

    cutoff = reference_time - timedelta(days=LOG_RETENTION_DAYS)
    retained = [history[0].sha]
    pruned: list[str] = []

    for commit in history[1:]:
        if commit.committed_at >= cutoff:
            retained.append(commit.sha)
        else:
            pruned.append(commit.sha)

    return LogHistoryRetentionPlan(
        branch=LOG_HISTORY_BRANCH,
        expected_head_sha=history[0].sha,
        reference_time=reference_time,
        cutoff=cutoff,
        retained_shas=tuple(retained),
        pruned_shas=tuple(pruned),
    )


def _bounded_history(commits: Iterable[LogHistoryCommit]) -> tuple[LogHistoryCommit, ...]:
    history: list[LogHistoryCommit] = []
    for commit in commits:
        if len(history) >= MAX_HISTORY_COMMITS:
            raise LogHistoryRetentionError("logs history exceeds the planning limit")
        if not isinstance(commit, LogHistoryCommit):
            raise LogHistoryRetentionError("logs history contains invalid commit evidence")
        history.append(commit)
    return tuple(history)


def _require_utc(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise LogHistoryRetentionError(f"{field} must be timezone-aware UTC")
