from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.log_history_evidence import LogHistoryRecord, validate_trusted_log_history_evidence
from ha_syncapp.log_retention_work import LOG_RETENTION_WORK_KIND, log_retention_work_key

TARGET = "Owner/Private-Home"
HEAD = "a" * 40
MID = "b" * 40
ROOT = "c" * 40
REFERENCE = datetime(2026, 9, 13, tzinfo=UTC)


def _evidence():
    return validate_trusted_log_history_evidence(
        branch_head=BranchHead(TARGET, 123, "logs", HEAD),
        records=(
            LogHistoryRecord(HEAD, REFERENCE - timedelta(days=1), (MID,)),
            LogHistoryRecord(MID, REFERENCE - timedelta(days=10), (ROOT,)),
            LogHistoryRecord(ROOT, REFERENCE - timedelta(days=45), ()),
        ),
        reference_time=REFERENCE,
    )


def test_log_retention_work_identity_is_deterministic_and_head_bound() -> None:
    evidence = _evidence()

    first = log_retention_work_key(evidence)
    second = log_retention_work_key(evidence)

    assert LOG_RETENTION_WORK_KIND == "logs_retention"
    assert first == second
    assert first.startswith(f"{HEAD}:")
    assert len(first) == 105


def test_log_retention_work_identity_rejects_non_logs_evidence() -> None:
    evidence = _evidence()
    forged = object.__new__(type(evidence))
    for name in ("target", "repository_id", "expected_head_sha", "commits", "plan"):
        object.__setattr__(forged, name, getattr(evidence, name))
    object.__setattr__(forged, "branch", "main")

    with pytest.raises(ValueError, match="identity"):
        log_retention_work_key(forged)
