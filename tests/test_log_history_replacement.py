from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.log_history_evidence import (
    LogHistoryRecord,
    TrustedLogHistoryEvidence,
    validate_trusted_log_history_evidence,
)
from ha_syncapp.log_history_prewrite import TrustedLogHistoryPrewrite
from ha_syncapp.log_history_replacement import (
    LogHistoryReplacementAuthorizationError,
    authorize_log_history_replacement,
)

REFERENCE = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)


def _sha(value: int) -> str:
    return f"{value:040x}"


def _evidence(*, expired_root: bool = True) -> TrustedLogHistoryEvidence:
    root_age = 40 if expired_root else 20
    return validate_trusted_log_history_evidence(
        branch_head=BranchHead(
            target="owner/private-repo",
            repository_id=123,
            branch="logs",
            commit_sha=_sha(3),
        ),
        records=(
            LogHistoryRecord(
                sha=_sha(3),
                committed_at=REFERENCE - timedelta(days=1),
                parent_shas=(_sha(2),),
            ),
            LogHistoryRecord(
                sha=_sha(2),
                committed_at=REFERENCE - timedelta(days=10),
                parent_shas=(_sha(1),),
            ),
            LogHistoryRecord(
                sha=_sha(1),
                committed_at=REFERENCE - timedelta(days=root_age),
                parent_shas=(),
            ),
        ),
        reference_time=REFERENCE,
    )


def _prewrite(evidence: TrustedLogHistoryEvidence) -> TrustedLogHistoryPrewrite:
    return TrustedLogHistoryPrewrite(
        target=evidence.target,
        repository_id=evidence.repository_id,
        branch=evidence.branch,
        expected_head_sha=evidence.expected_head_sha,
    )


def test_authorization_binds_exact_trusted_plan_and_prewrite() -> None:
    evidence = _evidence()

    authorization = authorize_log_history_replacement(
        evidence=evidence,
        prewrite=_prewrite(evidence),
    )

    assert authorization.target == "owner/private-repo"
    assert authorization.repository_id == 123
    assert authorization.branch == "logs"
    assert authorization.expected_head_sha == _sha(3)
    assert authorization.retained_shas == (_sha(3), _sha(2))
    assert authorization.pruned_shas == (_sha(1),)
    assert authorization.requires_replacement is True


def test_authorization_represents_noop_without_mutation_authority() -> None:
    evidence = _evidence(expired_root=False)

    authorization = authorize_log_history_replacement(
        evidence=evidence,
        prewrite=_prewrite(evidence),
    )

    assert authorization.retained_shas == (_sha(3), _sha(2), _sha(1))
    assert authorization.pruned_shas == ()
    assert authorization.requires_replacement is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target", "other/private-repo"),
        ("repository_id", 999),
        ("branch", "main"),
        ("expected_head_sha", _sha(9)),
    ],
)
def test_authorization_rejects_forged_prewrite_proof(field: str, value: object) -> None:
    evidence = _evidence()
    forged = replace(_prewrite(evidence), **{field: value})

    with pytest.raises(LogHistoryReplacementAuthorizationError):
        authorize_log_history_replacement(evidence=evidence, prewrite=forged)


def test_authorization_rejects_plan_with_overlapping_history() -> None:
    evidence = _evidence()
    forged_plan = replace(
        evidence.plan,
        retained_shas=(_sha(3), _sha(2)),
        pruned_shas=(_sha(2), _sha(1)),
    )
    forged = replace(evidence, plan=forged_plan)

    with pytest.raises(LogHistoryReplacementAuthorizationError, match="overlap"):
        authorize_log_history_replacement(evidence=forged, prewrite=_prewrite(evidence))


def test_authorization_rejects_plan_that_does_not_partition_evidence() -> None:
    evidence = _evidence()
    forged_plan = replace(
        evidence.plan,
        retained_shas=(_sha(3),),
        pruned_shas=(_sha(1),),
    )
    forged = replace(evidence, plan=forged_plan)

    with pytest.raises(LogHistoryReplacementAuthorizationError, match="does not match"):
        authorize_log_history_replacement(evidence=forged, prewrite=_prewrite(evidence))


def test_authorization_rejects_plan_without_current_head_retained() -> None:
    evidence = _evidence()
    forged_plan = replace(
        evidence.plan,
        retained_shas=(_sha(2),),
        pruned_shas=(_sha(1),),
    )
    forged = replace(evidence, plan=forged_plan)

    with pytest.raises(LogHistoryReplacementAuthorizationError, match="retained history is invalid"):
        authorize_log_history_replacement(evidence=forged, prewrite=_prewrite(evidence))
