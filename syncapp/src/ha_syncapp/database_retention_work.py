"""Durable work-state adapter for exact-head Recorder retention."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ha_syncapp.database_history_replace_transport import (
    DatabaseHistoryReplacementTransportError,
)
from ha_syncapp.state import StateError, StateStore, WorkItem

_WORK_KIND = "database-retention"
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_WORK_KEY = re.compile(r"^(\d{4}-\d{2}-\d{2}):([0-9a-f]{64})$")


class DatabaseRetentionWorkError(RuntimeError):
    """A durable Recorder-retention item cannot transition safely."""


@dataclass(frozen=True, slots=True)
class DatabaseRetentionWorkResult:
    """Persisted work outcome without credentials or staging details."""

    work: WorkItem
    replaced: bool | None


def database_retention_work_key(
    target: str,
    repository_id: int,
    expected_head_sha: str,
    retention_days: int,
    *,
    reference_time: datetime,
) -> str:
    """Return an exact-head/policy identity with a recoverable UTC decision day."""
    _validate_identity(target, repository_id, expected_head_sha, retention_days)
    reference_day = _reference_day(reference_time)
    payload = (
        f"{target.casefold()}\0{repository_id}\0database\0"
        f"{expected_head_sha}\0{retention_days}\0{reference_day}"
    ).encode()
    return f"{reference_day}:{hashlib.sha256(payload).hexdigest()}"


def enqueue_database_retention_work(
    store: StateStore,
    target: str,
    repository_id: int,
    expected_head_sha: str,
    retention_days: int,
    *,
    reference_time: datetime,
) -> WorkItem:
    """Idempotently enqueue retention for one verified database head, policy and UTC day."""
    if type(store) is not StateStore:
        raise DatabaseRetentionWorkError("database retention work state store is invalid")
    try:
        return store.enqueue_work(
            _WORK_KIND,
            database_retention_work_key(
                target,
                repository_id,
                expected_head_sha,
                retention_days,
                reference_time=reference_time,
            ),
        )
    except StateError as exc:
        raise DatabaseRetentionWorkError("database retention work could not be enqueued") from exc


def claim_database_retention_work(
    store: StateStore,
    *,
    now: datetime | None = None,
) -> WorkItem | None:
    """Atomically claim only the oldest eligible Recorder-retention item."""
    if type(store) is not StateStore:
        raise DatabaseRetentionWorkError("database retention work state store is invalid")
    try:
        return store.claim_work_kind(_WORK_KIND, now=now)
    except StateError as exc:
        raise DatabaseRetentionWorkError("database retention work claim is invalid") from exc


def execute_claimed_database_retention_work(
    store: StateStore,
    item: WorkItem,
    target: str,
    repository_id: int,
    expected_head_sha: str,
    retention_days: int,
    token: str,
    staging_root: Path,
) -> DatabaseRetentionWorkResult:
    """Execute one claimed retention item and persist retry/block/success state."""
    reference_time = _reference_time_from_work(item)
    _validate_claim(
        store,
        item,
        target,
        repository_id,
        expected_head_sha,
        retention_days,
        reference_time,
    )
    try:
        replaced = run_database_retention_cycle(
            target=target,
            repository_id=repository_id,
            expected_head_sha=expected_head_sha,
            retention_days=retention_days,
            reference_time=reference_time,
            token=token,
            staging_root=staging_root,
        )
    except DatabaseHistoryReplacementTransportError as error:
        try:
            failed = store.fail_work(item, transient=error.retryable)
        except StateError as exc:
            raise DatabaseRetentionWorkError(
                "database retention failure state could not be recorded"
            ) from exc
        return DatabaseRetentionWorkResult(failed, None)

    try:
        completed = store.complete_work(item)
    except StateError as exc:
        raise DatabaseRetentionWorkError(
            "database retention completion state could not be recorded"
        ) from exc
    return DatabaseRetentionWorkResult(completed, replaced)


def run_database_retention_cycle(
    *,
    target: str,
    repository_id: int,
    expected_head_sha: str,
    retention_days: int,
    reference_time: datetime,
    token: str,
    staging_root: Path,
) -> bool:
    """Fail closed until the isolated retention executor is supplied by the next TDD slice."""
    del (
        target,
        repository_id,
        expected_head_sha,
        retention_days,
        reference_time,
        token,
        staging_root,
    )
    raise DatabaseHistoryReplacementTransportError("database retention execution is unavailable")


def _validate_identity(
    target: str,
    repository_id: int,
    expected_head_sha: str,
    retention_days: int,
) -> None:
    if not isinstance(target, str) or not target.strip() or target != target.strip():
        raise DatabaseRetentionWorkError("database retention work identity is invalid")
    if type(repository_id) is not int or repository_id <= 0:
        raise DatabaseRetentionWorkError("database retention repository identity is invalid")
    if not isinstance(expected_head_sha, str) or _COMMIT_SHA.fullmatch(expected_head_sha) is None:
        raise DatabaseRetentionWorkError("database retention head identity is invalid")
    if type(retention_days) is not int or not 1 <= retention_days <= 365:
        raise DatabaseRetentionWorkError("database retention policy identity is invalid")


def _reference_day(reference_time: datetime) -> str:
    if (
        not isinstance(reference_time, datetime)
        or reference_time.tzinfo is None
        or reference_time.utcoffset() is None
    ):
        raise DatabaseRetentionWorkError("database retention reference time is invalid")
    return reference_time.astimezone(UTC).date().isoformat()


def _reference_time_from_work(item: WorkItem) -> datetime:
    if type(item) is not WorkItem or not isinstance(item.work_key, str):
        raise DatabaseRetentionWorkError("database retention work evidence is invalid")
    match = _WORK_KEY.fullmatch(item.work_key)
    if match is None:
        raise DatabaseRetentionWorkError("database retention work identity is invalid")
    try:
        return datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)
    except ValueError:
        raise DatabaseRetentionWorkError("database retention work identity is invalid") from None


def _validate_claim(
    store: StateStore,
    item: WorkItem,
    target: str,
    repository_id: int,
    expected_head_sha: str,
    retention_days: int,
    reference_time: datetime,
) -> None:
    if type(store) is not StateStore:
        raise DatabaseRetentionWorkError("database retention work state store is invalid")
    if type(item) is not WorkItem:
        raise DatabaseRetentionWorkError("database retention work evidence is invalid")
    if item.work_kind != _WORK_KIND or item.status != "running" or item.attempts < 1:
        raise DatabaseRetentionWorkError("database retention work is not an eligible claimed item")
    if item.work_key != database_retention_work_key(
        target,
        repository_id,
        expected_head_sha,
        retention_days,
        reference_time=reference_time,
    ):
        raise DatabaseRetentionWorkError(
            "database retention work identity does not match the trusted head"
        )
