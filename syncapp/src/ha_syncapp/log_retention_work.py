"""Durable identity, scheduling and execution boundary for logs history retention."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .github_repo import RepositoryVerificationError, fetch_trusted_branch_head
from .log_history_evidence import TrustedLogHistoryEvidence
from .log_history_prewrite import LogHistoryPrewriteError, reprove_log_history_prewrite
from .log_history_reader import LogHistoryReadError, fetch_trusted_log_history_evidence
from .log_history_replace_transport import (
    LogHistoryReplacementTransportError,
    build_log_history_replacement,
    replace_logs_history,
)
from .log_history_replacement import (
    LogHistoryReplacementAuthorizationError,
    authorize_log_history_replacement,
)
from .log_history_retention import LOG_HISTORY_BRANCH
from .log_retention_staging import LogRetentionStagingError, prepare_log_history_staging
from .state import StateError, StateStore, WorkItem

LOG_RETENTION_WORK_KIND = "logs_retention"
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class LogRetentionWorkError(ValueError):
    """Logs retention work cannot be identified or transitioned safely."""


class LogRetentionWorkDisposition(StrEnum):
    """Sanitized terminal disposition for one claimed logs-retention item."""

    NO_CHANGE = "no_change"
    REPLACED = "replaced"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class LogRetentionWorkResult:
    work: WorkItem
    disposition: LogRetentionWorkDisposition | None


@dataclass(frozen=True, slots=True)
class LogRetentionPassResult:
    recovered_interrupted: int
    processed: LogRetentionWorkResult | None


def log_retention_work_key(evidence: TrustedLogHistoryEvidence) -> str:
    """Bind durable work to one exact trusted head and fixed retention outcome.

    The head prefix is kept only in app-owned durable state so an interrupted
    replacement can reconstruct its original immutable history after the live
    branch has moved. Runtime recovery evidence deliberately excludes work keys.
    """

    if (
        type(evidence) is not TrustedLogHistoryEvidence
        or evidence.branch != LOG_HISTORY_BRANCH
        or evidence.plan.branch != LOG_HISTORY_BRANCH
        or not isinstance(evidence.target, str)
        or not evidence.target.strip()
        or type(evidence.repository_id) is not int
        or evidence.repository_id <= 0
        or _COMMIT_SHA.fullmatch(evidence.expected_head_sha) is None
        or not evidence.commits
        or evidence.commits[0].sha != evidence.expected_head_sha
        or evidence.plan.expected_head_sha != evidence.expected_head_sha
        or evidence.plan.retained_shas + evidence.plan.pruned_shas
        != tuple(commit.sha for commit in evidence.commits)
    ):
        raise LogRetentionWorkError("logs retention work identity is invalid")

    payload = json.dumps(
        {
            "branch": LOG_HISTORY_BRANCH,
            "head": evidence.expected_head_sha,
            "pruned": evidence.plan.pruned_shas,
            "repository_id": evidence.repository_id,
            "retained": evidence.plan.retained_shas,
            "retention_days": 30,
            "target": evidence.target.casefold(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"{evidence.expected_head_sha}:{digest}"


def discover_log_retention_work(
    store: StateStore,
    target: str,
    token: str,
    *,
    reference_time: datetime,
) -> WorkItem:
    """Read fresh trusted logs history and idempotently enqueue its exact policy outcome."""

    _validate_store(store)
    try:
        repository_id = store.repository_id(target)
        if repository_id is None:
            raise LogRetentionWorkError("logs retention repository is not pinned")
        evidence = fetch_trusted_log_history_evidence(
            target=target,
            token=token,
            expected_id=repository_id,
            reference_time=reference_time,
        )
        if (
            evidence.target.casefold() != target.casefold()
            or evidence.repository_id != repository_id
        ):
            raise LogRetentionWorkError("logs retention evidence describes another repository")
        return store.enqueue_work(
            LOG_RETENTION_WORK_KIND,
            log_retention_work_key(evidence),
            now=reference_time,
        )
    except LogRetentionWorkError:
        raise
    except (LogHistoryReadError, StateError):
        raise LogRetentionWorkError("logs retention discovery failed closed") from None


def claim_log_retention_work(store: StateStore, *, now: datetime | None = None) -> WorkItem | None:
    """Atomically claim only the oldest eligible logs-retention item."""

    _validate_store(store)
    try:
        return store.claim_work_kind(LOG_RETENTION_WORK_KIND, now=now)
    except StateError:
        raise LogRetentionWorkError("logs retention claim failed closed") from None


def run_log_retention_work_pass(
    store: StateStore,
    staging_root: Path,
    target: str,
    token: str,
    *,
    reference_time: datetime | None = None,
    recover_interrupted: bool = False,
) -> LogRetentionPassResult:
    """Discover current policy and process at most one durable logs-retention item."""

    _validate_store(store)
    current = reference_time or datetime.now(UTC)
    try:
        recovered = store.recover_interrupted_work(now=current) if recover_interrupted else 0
        item = claim_log_retention_work(store, now=current)
        if item is None:
            discover_log_retention_work(store, target, token, reference_time=current)
            item = claim_log_retention_work(store, now=current)
            if item is None:
                return LogRetentionPassResult(recovered, None)
        processed = _execute_claimed(
            store,
            item,
            staging_root,
            target,
            token,
            reference_time=current,
        )
        return LogRetentionPassResult(recovered, processed)
    except (StateError, LogRetentionWorkError):
        raise LogRetentionWorkError("logs retention pass failed closed") from None


def _execute_claimed(
    store: StateStore,
    item: WorkItem,
    staging_root: Path,
    target: str,
    token: str,
    *,
    reference_time: datetime,
) -> LogRetentionWorkResult:
    if (
        type(item) is not WorkItem
        or item.work_kind != LOG_RETENTION_WORK_KIND
        or item.status != "running"
        or item.attempts < 1
    ):
        raise LogRetentionWorkError("logs retention item is not claimed")

    repository: Path | None = None
    try:
        repository_id = store.repository_id(target)
        if repository_id is None:
            return _fail(store, item, transient=False, now=reference_time)

        intent = store.log_retention_intent(item.work_key)
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
                branch=LOG_HISTORY_BRANCH,
            )
            if current.commit_sha == intent.replacement_head_sha:
                baseline = store.synchronization_baseline(target, LOG_HISTORY_BRANCH)
                if baseline is None or baseline.snapshot_id != intent.snapshot_id:
                    return _fail(store, item, transient=False, now=reference_time)
                if baseline.commit_sha == intent.expected_head_sha:
                    store.record_synchronization_baseline(
                        target,
                        LOG_HISTORY_BRANCH,
                        intent.snapshot_id,
                        intent.replacement_head_sha,
                        synchronized_at=reference_time,
                    )
                elif baseline.commit_sha != intent.replacement_head_sha:
                    return _fail(store, item, transient=False, now=reference_time)
                completed = store.complete_work(item, now=reference_time)
                return LogRetentionWorkResult(completed, LogRetentionWorkDisposition.REPLACED)
            if current.commit_sha != intent.expected_head_sha:
                blocked = store.fail_work(item, transient=False, now=reference_time)
                return LogRetentionWorkResult(blocked, LogRetentionWorkDisposition.STALE)

        evidence = fetch_trusted_log_history_evidence(
            target=target,
            token=token,
            expected_id=repository_id,
            reference_time=item.created_at,
        )
        if item.work_key != log_retention_work_key(evidence):
            blocked = store.fail_work(item, transient=False, now=reference_time)
            return LogRetentionWorkResult(blocked, LogRetentionWorkDisposition.STALE)

        baseline = store.synchronization_baseline(target, LOG_HISTORY_BRANCH)
        if (
            baseline is None
            or baseline.target.casefold() != target.casefold()
            or baseline.branch != LOG_HISTORY_BRANCH
            or baseline.commit_sha != evidence.expected_head_sha
        ):
            return _fail(store, item, transient=False, now=reference_time)

        prewrite = reprove_log_history_prewrite(evidence=evidence, token=token)
        authorization = authorize_log_history_replacement(evidence=evidence, prewrite=prewrite)
        if not authorization.requires_replacement:
            completed = store.complete_work(item, now=reference_time)
            return LogRetentionWorkResult(completed, LogRetentionWorkDisposition.NO_CHANGE)

        repository = prepare_log_history_staging(
            evidence=evidence,
            staging_root=staging_root,
            token=token,
        )
        artifact = build_log_history_replacement(
            authorization=authorization,
            repository=repository,
        )
        persisted_intent = store.record_log_retention_intent(
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
        replace_logs_history(
            authorization=authorization,
            artifact=artifact,
            token=token,
        )
        store.record_synchronization_baseline(
            target,
            LOG_HISTORY_BRANCH,
            baseline.snapshot_id,
            artifact.replacement_head_sha,
            synchronized_at=reference_time,
        )
        completed = store.complete_work(item, now=reference_time)
        return LogRetentionWorkResult(completed, LogRetentionWorkDisposition.REPLACED)
    except LogHistoryReplacementTransportError as error:
        return _fail(store, item, transient=error.retryable, now=reference_time)
    except LogHistoryReadError as error:
        return _fail(store, item, transient=_read_failure_is_transient(error), now=reference_time)
    except LogHistoryPrewriteError as error:
        return _fail(
            store,
            item,
            transient="repository verification failed" in str(error),
            now=reference_time,
        )
    except LogRetentionStagingError as error:
        return _fail(
            store, item, transient=_staging_failure_is_transient(error), now=reference_time
        )
    except LogHistoryReplacementAuthorizationError:
        return _fail(store, item, transient=False, now=reference_time)
    except RepositoryVerificationError as error:
        return _fail(
            store, item, transient=_repository_failure_is_transient(error), now=reference_time
        )
    except StateError:
        raise LogRetentionWorkError("logs retention outcome could not be recorded") from None
    finally:
        if repository is not None:
            shutil.rmtree(repository, ignore_errors=True)


def _fail(
    store: StateStore,
    item: WorkItem,
    *,
    transient: bool,
    now: datetime | None = None,
) -> LogRetentionWorkResult:
    try:
        failed = store.fail_work(item, transient=transient, now=now)
    except StateError:
        raise LogRetentionWorkError("logs retention failure could not be recorded") from None
    return LogRetentionWorkResult(failed, None)


def _read_failure_is_transient(error: LogHistoryReadError) -> bool:
    message = str(error)
    return (
        "transport" in message
        or "HTTP 429" in message
        or any(f"HTTP {status}" in message for status in range(500, 600))
    )


def _repository_failure_is_transient(error: RepositoryVerificationError) -> bool:
    message = str(error)
    return (
        "transport" in message
        or "HTTP 429" in message
        or any(f"HTTP {status}" in message for status in range(500, 600))
    )


def _staging_failure_is_transient(error: LogRetentionStagingError) -> bool:
    message = str(error)
    return "transport" in message or "unavailable" in message


def _validate_store(store: StateStore) -> None:
    if type(store) is not StateStore:
        raise LogRetentionWorkError("logs retention state store is invalid")
