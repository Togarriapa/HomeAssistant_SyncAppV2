"""Durable identity boundary for recoverable generated-log history retention."""

from __future__ import annotations

import hashlib
import json
import re

from .log_history_evidence import TrustedLogHistoryEvidence
from .log_history_retention import LOG_HISTORY_BRANCH

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
            "reference_time": evidence.plan.reference_time.astimezone().isoformat(),
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
