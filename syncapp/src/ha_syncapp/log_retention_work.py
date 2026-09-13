"""Durable identity and scheduling boundary for recoverable logs history retention."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from .log_history_evidence import TrustedLogHistoryEvidence
from .log_history_reader import LogHistoryReadError, fetch_trusted_log_history_evidence
from .log_history_retention import LOG_HISTORY_BRANCH
from .state import StateError, StateStore, WorkItem

LOG_RETENTION_WORK_KIND = "logs_retention"
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class LogRetentionWorkError(ValueError):
    """Logs retention work cannot be identified or transitioned safely."""


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


def _validate_store(store: StateStore) -> None:
    if type(store) is not StateStore:
        raise LogRetentionWorkError("logs retention state store is invalid")
