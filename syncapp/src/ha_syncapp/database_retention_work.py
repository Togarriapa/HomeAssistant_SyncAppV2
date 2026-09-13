"""Durable, idempotent Recorder history-retention recovery work."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .database_history_evidence import TrustedDatabaseHistoryEvidence
from .database_history_prewrite import (
    DatabaseHistoryPrewriteError,
    reprove_database_history_prewrite,
)
from .database_history_reader import (
    DatabaseHistoryReadError,
    fetch_trusted_database_history_evidence,
)
from .database_history_replace_transport import (
    DatabaseHistoryReplacementTransportError,
    build_database_history_replacement,
    replace_database_history,
)
from .database_history_replacement import (
    DatabaseHistoryReplacementAuthorizationError,
    authorize_database_history_replacement,
)
from .database_retention_staging import (
    DatabaseRetentionStagingError,
    prepare_database_history_staging,
)
from .github_repo import RepositoryVerificationError, fetch_trusted_branch_head
from .state import StateError, StateStore, WorkItem

DATABASE_RETENTION_WORK_KIND = "database_retention"
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class DatabaseRetentionWorkError(RuntimeError):
    """Recorder retention work could not be scheduled or transitioned safely."""


class DatabaseRetentionWorkDisposition(StrEnum):
    """Sanitized terminal disposition for one claimed retention item."""

    NO_CHANGE = "no_change"
    REPLACED = "replaced"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class DatabaseRetentionWorkResult:
    work: WorkItem
    disposition: DatabaseRetentionWorkDisposition | None


@dataclass(frozen=True, slots=True)
class DatabaseRetentionPassResult:
    recovered_interrupted: int
    processed: DatabaseRetentionWorkResult | None


def database_retention_work_key(evidence: TrustedDatabaseHistoryEvidence) -> str:
    """Hash one exact trusted head and deterministic policy outcome."""

    if (
        type(evidence) is not TrustedDatabaseHistoryEvidence
        or evidence.branch != "database"
        or evidence.plan.branch != "database"
        or not isinstance(evidence.target, str)
        or not evidence.target.strip()
        or not evidence.records
        or evidence.records[0].sha != evidence.expected_head_sha
        or _COMMIT_SHA.fullmatch(evidence.expected_head_sha) is None
        or type(evidence.repository_id) is not int
        or evidence.repository_id <= 0
        or type(evidence.plan.retention_days) is not int
        or not 1 <= evidence.plan.retention_days <= 365
        or evidence.plan.retained_identities + evidence.plan.prunable_identities
        != tuple(record.sha for record in evidence.records)
    ):
        raise DatabaseRetentionWorkError("database retention work identity is invalid")
    payload = json.dumps(
        {
            "branch": evidence.branch,
            "head": evidence.expected_head_sha,
            "prunable": evidence.plan.prunable_identities,
            "repository_id": evidence.repository_id,
            "retained": evidence.plan.retained_identities,
            "retention_days": evidence.plan.retention_days,
            "target": evidence.target.casefold(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def discover_database_retention_work(
    store: StateStore,
    target: str,
    token: str,
    *,
    retention_days: int,
    reference_time: datetime,
) -> WorkItem:
    """Read fresh trusted history and idempotently enqueue its exact policy outcome."""

    _validate_store(store)
    try:
        repository_id = store.repository_id(target)
        if repository_id is None:
            raise DatabaseRetentionWorkError("database retention repository is not pinned")
        evidence = fetch_trusted_database_history_evidence(
            target=target,
            token=token,
            expected_id=repository_id,
            reference_time=reference_time,
            retention_days=retention_days,
        )
        if (
            evidence.target.casefold() != target.casefold()
            or evidence.repository_id != repository_id
        ):
            raise DatabaseRetentionWorkError(
                "database retention evidence describes another repository"
            )
        return store.enqueue_work(
            DATABASE_RETENTION_WORK_KIND,
            database_retention_work_key(evidence),
            now=reference_time,
        )
    except DatabaseRetentionWorkError:
        raise
    except (DatabaseHistoryReadError, StateError):
        raise DatabaseRetentionWorkError("database retention discovery failed closed") from None


def claim_database_retention_work(
    store: StateStore, *, now: datetime | None = None
) -> WorkItem | None:
    """Atomically claim only the oldest eligible Recorder-retention item."""

    _validate_store(store)
    try:
        return store.claim_work_kind(DATABASE_RETENTION_WORK_KIND, now=now)
    except StateError:
        raise DatabaseRetentionWorkError("database retention claim failed closed") from None


def run_database_retention_work_pass(
    store: StateStore,
    staging_root: Path,
    target: str,
    token: str,
    *,
    retention_days: int,
    reference_time: datetime | None = None,
    recover_interrupted: bool = False,
) -> DatabaseRetentionPassResult:
    """Discover current policy and process at most one durable retention item."""

    _validate_store(store)
    current = reference_time or datetime.now(UTC)
    try:
        recovered = store.recover_interrupted_work(now=current) if recover_interrupted else 0
        item = claim_database_retention_work(store, now=current)
        if item is None:
            discover_database_retention_work(
                store,
                target,
                token,
                retention_days=retention_days,
                reference_time=current,
            )
            item = claim_database_retention_work(store, now=current)
            if item is None:
                return DatabaseRetentionPassResult(recovered, None)
        processed = _execute_claimed(
            store,
            item,
            staging_root,
            target,
            token,
            retention_days=retention_days,
            reference_time=current,
        )
        return DatabaseRetentionPassResult(recovered, processed)
    except (StateError, DatabaseRetentionWorkError):
        raise DatabaseRetentionWorkError("database retention pass failed closed") from None


def _execute_claimed(
    store: StateStore,
    item: WorkItem,
    staging_root: Path,
    target: str,
    token: str,
    *,
    retention_days: int,
    reference_time: datetime,
) -> DatabaseRetentionWorkResult:
    if (
        type(item) is not WorkItem
        or item.work_kind != DATABASE_RETENTION_WORK_KIND
        or item.status != "running"
        or item.attempts < 1
    ):
        raise DatabaseRetentionWorkError("database retention item is not claimed")

    repository: Path | None = None
    try:
        repository_id = store.repository_id(target)
        if repository_id is None:
            return _fail(store, item, transient=False)
        intent = store.database_retention_intent(item.work_key)
        if intent is not None:
            if (
                intent.target.casefold() != target.casefold()
                or intent.repository_id != repository_id
            ):
                return _fail(store, item, transient=False, now=reference_time)
            current = fetch_trusted_branch_head(
                target,
                token,
                expected_id=repository_id,
                branch="database",
            )
            if current.commit_sha == intent.replacement_head_sha:
                baseline = store.synchronization_baseline(target, "database")
                if baseline is None or baseline.snapshot_id != intent.snapshot_id:
                    return _fail(store, item, transient=False, now=reference_time)
                if baseline.commit_sha == intent.expected_head_sha:
                    store.record_synchronization_baseline(
                        target,
                        "database",
                        intent.snapshot_id,
                        intent.replacement_head_sha,
                        synchronized_at=reference_time,
                    )
                elif baseline.commit_sha != intent.replacement_head_sha:
                    return _fail(store, item, transient=False, now=reference_time)
                completed = store.complete_work(item, now=reference_time)
                return DatabaseRetentionWorkResult(
                    completed, DatabaseRetentionWorkDisposition.REPLACED
                )
            if current.commit_sha != intent.expected_head_sha:
                blocked = store.fail_work(item, transient=False, now=reference_time)
                return DatabaseRetentionWorkResult(blocked, DatabaseRetentionWorkDisposition.STALE)
        evidence = fetch_trusted_database_history_evidence(
            target=target,
            token=token,
            expected_id=repository_id,
            reference_time=item.created_at,
            retention_days=retention_days,
        )
        if item.work_key != database_retention_work_key(evidence):
            blocked = store.fail_work(item, transient=False, now=reference_time)
            return DatabaseRetentionWorkResult(blocked, DatabaseRetentionWorkDisposition.STALE)
        baseline = store.synchronization_baseline(target, "database")
        if (
            baseline is None
            or baseline.target.casefold() != target.casefold()
            or baseline.branch != "database"
            or baseline.commit_sha != evidence.expected_head_sha
        ):
            return _fail(store, item, transient=False, now=reference_time)
        prewrite = reprove_database_history_prewrite(evidence=evidence, token=token)
        authorization = authorize_database_history_replacement(evidence=evidence, prewrite=prewrite)
        if not authorization.requires_replacement:
            replace_database_history(authorization=authorization)
            completed = store.complete_work(item, now=reference_time)
            return DatabaseRetentionWorkResult(
                completed, DatabaseRetentionWorkDisposition.NO_CHANGE
            )

        repository = prepare_database_history_staging(
            evidence=evidence,
            staging_root=staging_root,
            token=token,
        )
        artifact = build_database_history_replacement(
            authorization=authorization,
            repository=repository,
        )
        persisted_intent = store.record_database_retention_intent(
            item,
            target,
            repository_id,
            authorization.expected_head_sha,
            artifact.replacement_head_sha,
            baseline.snapshot_id,
            recorded_at=item.created_at,
        )
        if persisted_intent.replacement_head_sha != artifact.replacement_head_sha:
            return _fail(store, item, transient=False, now=reference_time)
        replace_database_history(
            authorization=authorization,
            artifact=artifact,
            token=token,
        )
        store.record_synchronization_baseline(
            target,
            "database",
            baseline.snapshot_id,
            artifact.replacement_head_sha,
            synchronized_at=reference_time,
        )
        completed = store.complete_work(item, now=reference_time)
        return DatabaseRetentionWorkResult(completed, DatabaseRetentionWorkDisposition.REPLACED)
    except DatabaseHistoryReplacementTransportError as error:
        return _fail(store, item, transient=error.retryable, now=reference_time)
    except DatabaseHistoryReadError as error:
        transient = (
            "transport" in str(error)
            or "HTTP 429" in str(error)
            or any(f"HTTP {status}" in str(error) for status in range(500, 600))
        )
        return _fail(store, item, transient=transient, now=reference_time)
    except DatabaseHistoryPrewriteError as error:
        return _fail(
            store,
            item,
            transient="repository verification failed" in str(error),
            now=reference_time,
        )
    except DatabaseRetentionStagingError as error:
        return _fail(
            store,
            item,
            transient=_staging_failure_is_transient(error),
            now=reference_time,
        )
    except DatabaseHistoryReplacementAuthorizationError:
        return _fail(store, item, transient=False, now=reference_time)
    except RepositoryVerificationError as error:
        return _fail(
            store,
            item,
            transient=_repository_failure_is_transient(error),
            now=reference_time,
        )
    except StateError:
        raise DatabaseRetentionWorkError(
            "database retention outcome could not be recorded"
        ) from None
    finally:
        if repository is not None:
            shutil.rmtree(repository, ignore_errors=True)


def _fail(
    store: StateStore,
    item: WorkItem,
    *,
    transient: bool,
    now: datetime | None = None,
) -> DatabaseRetentionWorkResult:
    try:
        failed = store.fail_work(item, transient=transient, now=now)
    except StateError:
        raise DatabaseRetentionWorkError(
            "database retention failure could not be recorded"
        ) from None
    return DatabaseRetentionWorkResult(failed, None)


def _validate_store(store: StateStore) -> None:
    if type(store) is not StateStore:
        raise DatabaseRetentionWorkError("database retention state store is invalid")


def _repository_failure_is_transient(error: RepositoryVerificationError) -> bool:
    message = str(error)
    return (
        "transport" in message
        or "HTTP 429" in message
        or any(f"HTTP {status}" in message for status in range(500, 600))
    )


def _staging_failure_is_transient(error: DatabaseRetentionStagingError) -> bool:
    message = str(error)
    return "transport" in message or "unavailable" in message
