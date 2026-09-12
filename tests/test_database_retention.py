from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from ha_syncapp.database_retention import (
    MAX_DATABASE_SNAPSHOTS,
    DatabaseRetentionError,
    DatabaseSnapshotEvidence,
    plan_database_retention,
)

REFERENCE = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def _snapshot(identity: str, age: timedelta) -> DatabaseSnapshotEvidence:
    return DatabaseSnapshotEvidence(
        identity=identity,
        created_at=REFERENCE - age,
    )


def test_plan_retains_snapshots_inside_configured_window() -> None:
    snapshots = (
        _snapshot("snapshot-4", timedelta(days=1)),
        _snapshot("snapshot-3", timedelta(days=7)),
        _snapshot("snapshot-2", timedelta(days=7, seconds=1)),
        _snapshot("snapshot-1", timedelta(days=30)),
    )

    plan = plan_database_retention(
        branch="database",
        snapshots=snapshots,
        reference_time=REFERENCE,
        retention_days=7,
    )

    assert plan.branch == "database"
    assert plan.reference_time == REFERENCE
    assert plan.retention_days == 7
    assert plan.cutoff == REFERENCE - timedelta(days=7)
    assert plan.retained_identities == ("snapshot-4", "snapshot-3")
    assert plan.prunable_identities == ("snapshot-2", "snapshot-1")


def test_plan_always_retains_newest_snapshot_when_all_are_expired() -> None:
    plan = plan_database_retention(
        branch="database",
        snapshots=(
            _snapshot("snapshot-2", timedelta(days=8)),
            _snapshot("snapshot-1", timedelta(days=20)),
        ),
        reference_time=REFERENCE,
        retention_days=7,
    )

    assert plan.retained_identities == ("snapshot-2",)
    assert plan.prunable_identities == ("snapshot-1",)


def test_plan_is_deterministic() -> None:
    snapshots = (
        _snapshot("snapshot-3", timedelta(days=1)),
        _snapshot("snapshot-2", timedelta(days=3)),
        _snapshot("snapshot-1", timedelta(days=10)),
    )

    first = plan_database_retention(
        branch="database",
        snapshots=snapshots,
        reference_time=REFERENCE,
        retention_days=7,
    )
    second = plan_database_retention(
        branch="database",
        snapshots=snapshots,
        reference_time=REFERENCE,
        retention_days=7,
    )

    assert first == second


@pytest.mark.parametrize(
    "branch", ["main", "candidate", "runtime", "logs", "Database", ""]
)
def test_plan_rejects_every_non_database_branch(branch: str) -> None:
    with pytest.raises(DatabaseRetentionError, match="branch"):
        plan_database_retention(
            branch=branch,
            snapshots=(_snapshot("snapshot-1", timedelta(days=1)),),
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_plan_requires_at_least_one_snapshot() -> None:
    with pytest.raises(DatabaseRetentionError, match="snapshot"):
        plan_database_retention(
            branch="database",
            snapshots=(),
            reference_time=REFERENCE,
            retention_days=7,
        )


@pytest.mark.parametrize("retention_days", [0, -1, 366, True, False])
def test_plan_rejects_retention_outside_config_contract(retention_days: int) -> None:
    with pytest.raises(DatabaseRetentionError, match="retention"):
        plan_database_retention(
            branch="database",
            snapshots=(_snapshot("snapshot-1", timedelta(days=1)),),
            reference_time=REFERENCE,
            retention_days=retention_days,
        )


def test_plan_rejects_duplicate_snapshot_identity() -> None:
    snapshots = (
        _snapshot("snapshot-1", timedelta(days=1)),
        _snapshot("snapshot-1", timedelta(days=2)),
    )

    with pytest.raises(DatabaseRetentionError, match="duplicate"):
        plan_database_retention(
            branch="database",
            snapshots=snapshots,
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_plan_rejects_history_that_is_not_newest_first() -> None:
    snapshots = (
        _snapshot("snapshot-1", timedelta(days=2)),
        _snapshot("snapshot-2", timedelta(days=1)),
    )

    with pytest.raises(DatabaseRetentionError, match="newest-first"):
        plan_database_retention(
            branch="database",
            snapshots=snapshots,
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_plan_rejects_future_snapshot_timestamp() -> None:
    snapshot = DatabaseSnapshotEvidence(
        identity="snapshot-1",
        created_at=REFERENCE + timedelta(seconds=1),
    )

    with pytest.raises(DatabaseRetentionError, match="future"):
        plan_database_retention(
            branch="database",
            snapshots=(snapshot,),
            reference_time=REFERENCE,
            retention_days=7,
        )


@pytest.mark.parametrize(
    "identity", ["", " snapshot", "snapshot ", "snapshot/name", "snapshot\nname"]
)
def test_plan_rejects_invalid_snapshot_identity(identity: str) -> None:
    snapshot = DatabaseSnapshotEvidence(
        identity=identity,
        created_at=REFERENCE - timedelta(days=1),
    )

    with pytest.raises(DatabaseRetentionError, match="identity"):
        plan_database_retention(
            branch="database",
            snapshots=(snapshot,),
            reference_time=REFERENCE,
            retention_days=7,
        )


@pytest.mark.parametrize(
    "bad_time",
    [
        datetime(2026, 9, 12, 12, 0),
        datetime(2026, 9, 12, 13, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_plan_requires_reference_time_in_utc(bad_time: datetime) -> None:
    with pytest.raises(DatabaseRetentionError, match="reference_time"):
        plan_database_retention(
            branch="database",
            snapshots=(_snapshot("snapshot-1", timedelta(days=1)),),
            reference_time=bad_time,
            retention_days=7,
        )


def test_plan_requires_snapshot_times_in_utc() -> None:
    snapshot = DatabaseSnapshotEvidence(
        identity="snapshot-1",
        created_at=datetime(2026, 9, 11, 12, 0),
    )

    with pytest.raises(DatabaseRetentionError, match="created_at"):
        plan_database_retention(
            branch="database",
            snapshots=(snapshot,),
            reference_time=REFERENCE,
            retention_days=7,
        )


def test_plan_bounds_snapshot_input() -> None:
    snapshots = tuple(
        DatabaseSnapshotEvidence(
            identity=f"snapshot-{index}",
            created_at=REFERENCE - timedelta(seconds=index),
        )
        for index in range(MAX_DATABASE_SNAPSHOTS + 1)
    )

    with pytest.raises(DatabaseRetentionError, match="limit"):
        plan_database_retention(
            branch="database",
            snapshots=snapshots,
            reference_time=REFERENCE,
            retention_days=7,
        )
