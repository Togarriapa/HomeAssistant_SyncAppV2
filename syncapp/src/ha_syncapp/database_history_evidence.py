from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from ha_syncapp.database_retention import (
    DATABASE_BRANCH,
    MAX_DATABASE_SNAPSHOTS,
    DatabaseRetentionError,
    DatabaseRetentionPlan,
    DatabaseSnapshotEvidence,
    plan_database_retention,
)
from ha_syncapp.github_repo import BranchHead

_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class DatabaseHistoryEvidenceError(ValueError):
    """Raised when trusted Recorder history evidence is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class DatabaseHistoryRecord:
    """Immutable metadata for one commit in generated database history."""

    sha: str
    committed_at: datetime
    parent_shas: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrustedDatabaseHistoryEvidence:
    """Complete database history bound to one verified private Repo B head."""

    target: str
    repository_id: int
    branch: str
    expected_head_sha: str
    records: tuple[DatabaseHistoryRecord, ...]
    plan: DatabaseRetentionPlan


def validate_trusted_database_history_evidence(
    *,
    branch_head: BranchHead,
    records: Iterable[DatabaseHistoryRecord],
    reference_time: datetime,
    retention_days: int,
) -> TrustedDatabaseHistoryEvidence:
    """Bind a deterministic retention plan to complete trusted database history."""

    if branch_head.branch != DATABASE_BRANCH:
        raise DatabaseHistoryEvidenceError("history evidence is restricted to the database branch")
    if (
        not isinstance(branch_head.target, str)
        or not branch_head.target.strip()
        or type(branch_head.repository_id) is not int
        or branch_head.repository_id <= 0
    ):
        raise DatabaseHistoryEvidenceError("trusted repository identity is invalid")
    if not isinstance(branch_head.commit_sha, str) or _SHA_PATTERN.fullmatch(branch_head.commit_sha) is None:
        raise DatabaseHistoryEvidenceError("trusted database head is invalid")

    history = _bounded_records(records)
    if not history:
        raise DatabaseHistoryEvidenceError("trusted database history must not be empty")
    if history[0].sha != branch_head.commit_sha:
        raise DatabaseHistoryEvidenceError("trusted database history does not match the verified head")

    for index, record in enumerate(history):
        if not isinstance(record.sha, str) or _SHA_PATTERN.fullmatch(record.sha) is None:
            raise DatabaseHistoryEvidenceError("database history contains an invalid commit SHA")
        if not isinstance(record.parent_shas, tuple) or any(
            not isinstance(parent, str) or _SHA_PATTERN.fullmatch(parent) is None
            for parent in record.parent_shas
        ):
            raise DatabaseHistoryEvidenceError("database history contains an invalid parent SHA")
        if len(record.parent_shas) > 1:
            raise DatabaseHistoryEvidenceError("database history must be linear")

        is_root = index == len(history) - 1
        if is_root:
            if record.parent_shas:
                raise DatabaseHistoryEvidenceError(
                    "database history evidence must be complete to the root"
                )
            continue

        if record.parent_shas != (history[index + 1].sha,):
            raise DatabaseHistoryEvidenceError("database history parent chain is inconsistent")

    snapshots = tuple(
        DatabaseSnapshotEvidence(identity=record.sha, created_at=record.committed_at)
        for record in history
    )
    try:
        plan = plan_database_retention(
            branch=DATABASE_BRANCH,
            snapshots=snapshots,
            reference_time=reference_time,
            retention_days=retention_days,
        )
    except DatabaseRetentionError as error:
        raise DatabaseHistoryEvidenceError(str(error)) from None

    return TrustedDatabaseHistoryEvidence(
        target=branch_head.target,
        repository_id=branch_head.repository_id,
        branch=DATABASE_BRANCH,
        expected_head_sha=branch_head.commit_sha,
        records=history,
        plan=plan,
    )


def _bounded_records(
    records: Iterable[DatabaseHistoryRecord],
) -> tuple[DatabaseHistoryRecord, ...]:
    history: list[DatabaseHistoryRecord] = []
    for record in records:
        if len(history) >= MAX_DATABASE_SNAPSHOTS:
            raise DatabaseHistoryEvidenceError("database history exceeds the evidence limit")
        if not isinstance(record, DatabaseHistoryRecord):
            raise DatabaseHistoryEvidenceError("database history contains invalid evidence")
        history.append(record)
    return tuple(history)
