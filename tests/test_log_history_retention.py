from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ha_syncapp.log_history_retention import (
    MAX_HISTORY_COMMITS,
    LogHistoryCommit,
    LogHistoryRetentionError,
    plan_log_history_retention,
)


REFERENCE = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _sha(value: int) -> str:
    return f"{value:040x}"


def _commit(value: int, age: timedelta) -> LogHistoryCommit:
    return LogHistoryCommit(sha=_sha(value), committed_at=REFERENCE - age)


def test_plan_retains_head_and_commits_inside_30_day_window() -> None:
    commits = (
        _commit(4, timedelta(days=1)),
        _commit(3, timedelta(days=30)),
        _commit(2, timedelta(days=30, seconds=1)),
        _commit(1, timedelta(days=90)),
    )

    plan = plan_log_history_retention(
        branch="logs",
        commits=commits,
        reference_time=REFERENCE,
    )

    assert plan.branch == "logs"
    assert plan.expected_head_sha == _sha(4)
    assert plan.cutoff == REFERENCE - timedelta(days=30)
    assert plan.retained_shas == (_sha(4), _sha(3))
    assert plan.pruned_shas == (_sha(2), _sha(1))


def test_plan_always_retains_current_head_when_entire_history_is_expired() -> None:
    commits = (
        _commit(2, timedelta(days=31)),
        _commit(1, timedelta(days=60)),
    )

    plan = plan_log_history_retention(
        branch="logs",
        commits=commits,
        reference_time=REFERENCE,
    )

    assert plan.retained_shas == (_sha(2),)
    assert plan.pruned_shas == (_sha(1),)


def test_plan_is_deterministic() -> None:
    commits = (
        _commit(3, timedelta(days=2)),
        _commit(2, timedelta(days=7)),
        _commit(1, timedelta(days=45)),
    )

    first = plan_log_history_retention(
        branch="logs",
        commits=commits,
        reference_time=REFERENCE,
    )
    second = plan_log_history_retention(
        branch="logs",
        commits=commits,
        reference_time=REFERENCE,
    )

    assert first == second


@pytest.mark.parametrize("branch", ["main", "candidate", "runtime", "database", "Logs", ""])
def test_plan_rejects_every_non_logs_branch(branch: str) -> None:
    with pytest.raises(LogHistoryRetentionError, match="branch"):
        plan_log_history_retention(
            branch=branch,
            commits=(_commit(1, timedelta(days=1)),),
            reference_time=REFERENCE,
        )


def test_plan_requires_at_least_one_commit() -> None:
    with pytest.raises(LogHistoryRetentionError, match="history"):
        plan_log_history_retention(
            branch="logs",
            commits=(),
            reference_time=REFERENCE,
        )


def test_plan_rejects_duplicate_commit_identity() -> None:
    commits = (
        _commit(1, timedelta(days=1)),
        _commit(1, timedelta(days=2)),
    )

    with pytest.raises(LogHistoryRetentionError, match="duplicate"):
        plan_log_history_retention(
            branch="logs",
            commits=commits,
            reference_time=REFERENCE,
        )


def test_plan_rejects_history_that_is_not_newest_first() -> None:
    commits = (
        _commit(1, timedelta(days=2)),
        _commit(2, timedelta(days=1)),
    )

    with pytest.raises(LogHistoryRetentionError, match="newest-first"):
        plan_log_history_retention(
            branch="logs",
            commits=commits,
            reference_time=REFERENCE,
        )


def test_plan_rejects_future_commit_timestamp() -> None:
    future = LogHistoryCommit(
        sha=_sha(1),
        committed_at=REFERENCE + timedelta(seconds=1),
    )

    with pytest.raises(LogHistoryRetentionError, match="future"):
        plan_log_history_retention(
            branch="logs",
            commits=(future,),
            reference_time=REFERENCE,
        )


@pytest.mark.parametrize(
    "bad_sha",
    [
        "abc",
        "A" * 40,
        "g" * 40,
        "0" * 39,
        "0" * 41,
    ],
)
def test_plan_rejects_invalid_commit_identity(bad_sha: str) -> None:
    commit = LogHistoryCommit(
        sha=bad_sha,
        committed_at=REFERENCE - timedelta(days=1),
    )

    with pytest.raises(LogHistoryRetentionError, match="SHA"):
        plan_log_history_retention(
            branch="logs",
            commits=(commit,),
            reference_time=REFERENCE,
        )


@pytest.mark.parametrize(
    "bad_time",
    [
        datetime(2026, 9, 12, 12, 0),
        datetime(2026, 9, 12, 13, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_plan_requires_reference_time_in_utc(bad_time: datetime) -> None:
    with pytest.raises(LogHistoryRetentionError, match="reference_time"):
        plan_log_history_retention(
            branch="logs",
            commits=(_commit(1, timedelta(days=1)),),
            reference_time=bad_time,
        )


def test_plan_requires_commit_times_in_utc() -> None:
    commit = LogHistoryCommit(
        sha=_sha(1),
        committed_at=datetime(2026, 9, 11, 12, 0),
    )

    with pytest.raises(LogHistoryRetentionError, match="committed_at"):
        plan_log_history_retention(
            branch="logs",
            commits=(commit,),
            reference_time=REFERENCE,
        )


def test_plan_bounds_history_input() -> None:
    commits = tuple(
        LogHistoryCommit(
            sha=_sha(index + 1),
            committed_at=REFERENCE - timedelta(seconds=index),
        )
        for index in range(MAX_HISTORY_COMMITS + 1)
    )

    with pytest.raises(LogHistoryRetentionError, match="limit"):
        plan_log_history_retention(
            branch="logs",
            commits=commits,
            reference_time=REFERENCE,
        )
