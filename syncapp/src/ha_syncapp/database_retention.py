from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

DATABASE_BRANCH = "database"
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365
MAX_DATABASE_SNAPSHOTS = 4096
_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DatabaseRetentionError(ValueError):
    """Raised when Recorder snapshot evidence cannot be retained safely."""


@dataclass(frozen=True, slots=True)
class DatabaseSnapshotEvidence:
    """Deterministic metadata for one generated Recorder snapshot."""

    identity: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DatabaseRetentionPlan:
    """A side-effect-free classification of generated database snapshots."""

    branch: str
    reference_time: datetime
    retention_days: int
    cutoff: datetime
    retained_identities: tuple[str, ...]
    prunable_identities: tuple[str, ...]


def plan_database_retention(
    *,
    branch: str,
    snapshots: Iterable[DatabaseSnapshotEvidence],
    reference_time: datetime,
    retention_days: int,
) -> DatabaseRetentionPlan:
    """Classify generated ``database`` snapshots without mutating storage or Git."""

    if branch != DATABASE_BRANCH:
        raise DatabaseRetentionError("snapshot retention is restricted to the database branch")
    if (
        type(retention_days) is not int
        or retention_days < MIN_RETENTION_DAYS
        or retention_days > MAX_RETENTION_DAYS
    ):
        raise DatabaseRetentionError("retention days are outside the configured safety bounds")
    if not isinstance(reference_time, datetime):
        raise DatabaseRetentionError("reference_time must be timezone-aware UTC")
    _require_utc(reference_time, field="reference_time")

    evidence = _bounded_snapshots(snapshots)
    if not evidence:
        raise DatabaseRetentionError("database snapshot evidence must contain a newest snapshot")

    seen: set[str] = set()
    previous_time: datetime | None = None
    for snapshot in evidence:
        if (
            not isinstance(snapshot.identity, str)
            or _IDENTITY_PATTERN.fullmatch(snapshot.identity) is None
        ):
            raise DatabaseRetentionError("database snapshot contains an invalid identity")
        if snapshot.identity in seen:
            raise DatabaseRetentionError("database snapshot evidence contains a duplicate identity")
        seen.add(snapshot.identity)

        if not isinstance(snapshot.created_at, datetime):
            raise DatabaseRetentionError("created_at must be timezone-aware UTC")
        _require_utc(snapshot.created_at, field="created_at")
        if snapshot.created_at > reference_time:
            raise DatabaseRetentionError("database snapshot evidence contains a future timestamp")
        if previous_time is not None and snapshot.created_at > previous_time:
            raise DatabaseRetentionError("database snapshot evidence must be ordered newest-first")
        previous_time = snapshot.created_at

    cutoff = reference_time - timedelta(days=retention_days)
    retained = [evidence[0].identity]
    prunable: list[str] = []

    for snapshot in evidence[1:]:
        if snapshot.created_at >= cutoff:
            retained.append(snapshot.identity)
        else:
            prunable.append(snapshot.identity)

    return DatabaseRetentionPlan(
        branch=DATABASE_BRANCH,
        reference_time=reference_time,
        retention_days=retention_days,
        cutoff=cutoff,
        retained_identities=tuple(retained),
        prunable_identities=tuple(prunable),
    )


def _bounded_snapshots(
    snapshots: Iterable[DatabaseSnapshotEvidence],
) -> tuple[DatabaseSnapshotEvidence, ...]:
    evidence: list[DatabaseSnapshotEvidence] = []
    for snapshot in snapshots:
        if len(evidence) >= MAX_DATABASE_SNAPSHOTS:
            raise DatabaseRetentionError("database snapshot evidence exceeds the planning limit")
        if not isinstance(snapshot, DatabaseSnapshotEvidence):
            raise DatabaseRetentionError("database snapshot evidence contains an invalid record")
        evidence.append(snapshot)
    return tuple(evidence)


def _require_utc(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DatabaseRetentionError(f"{field} must be timezone-aware UTC")
