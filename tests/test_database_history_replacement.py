from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.database_history_evidence import (
    DatabaseHistoryRecord,
    TrustedDatabaseHistoryEvidence,
    validate_trusted_database_history_evidence,
)
from ha_syncapp.database_history_replacement import (
    DatabaseHistoryReplacementAuthorizationError,
    TrustedDatabaseHistoryPrewrite,
    authorize_database_history_replacement,
)
from ha_syncapp.github_repo import BranchHead

REFERENCE = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)


def _sha(value: int) -> str:
    return f"{value:040x}"


def _evidence(*, expired_root: bool = True) -> TrustedDatabaseHistoryEvidence:
    root_age = 10 if expired_root else 6
    return validate_trusted_database_history_evidence(
        branch_head=BranchHead(
            target="owner/private-repo",
            repository_id=123,
            branch="database",
            commit_sha=_sha(3),
        ),
        records=(
            DatabaseHistoryRecord(_sha(3), REFERENCE - timedelta(days=1), (_sha(2),)),
            DatabaseHistoryRecord(_sha(2), REFERENCE - timedelta(days=5), (_sha(1),)),
            DatabaseHistoryRecord(_sha(1), REFERENCE - timedelta(days=root_age), ()),
        ),
        reference_time=REFERENCE,
        retention_days=7,
    )


def _prewrite(evidence: TrustedDatabaseHistoryEvidence) -> TrustedDatabaseHistoryPrewrite:
    return TrustedDatabaseHistoryPrewrite(
        target=evidence.target,
        repository_id=evidence.repository_id,
        branch=evidence.branch,
        expected_head_sha=evidence.expected_head_sha,
    )


def test_authorization_binds_exact_database_plan_and_fresh_proof() -> None:
    evidence = _evidence()
    authorization = authorize_database_history_replacement(
        evidence=evidence, prewrite=_prewrite(evidence)
    )
    assert authorization.target == "owner/private-repo"
    assert authorization.repository_id == 123
    assert authorization.branch == "database"
    assert authorization.expected_head_sha == _sha(3)
    assert authorization.retained_shas == (_sha(3), _sha(2))
    assert authorization.pruned_shas == (_sha(1),)
    assert authorization.requires_replacement is True


def test_authorization_represents_noop_without_replacement_authority() -> None:
    evidence = _evidence(expired_root=False)
    authorization = authorize_database_history_replacement(
        evidence=evidence, prewrite=_prewrite(evidence)
    )
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
def test_authorization_rejects_forged_prewrite(field: str, value: object) -> None:
    evidence = _evidence()
    forged = replace(_prewrite(evidence), **{field: value})
    with pytest.raises(DatabaseHistoryReplacementAuthorizationError):
        authorize_database_history_replacement(evidence=evidence, prewrite=forged)


def test_authorization_rejects_plan_overlap() -> None:
    evidence = _evidence()
    forged = replace(
        evidence,
        plan=replace(
            evidence.plan,
            retained_identities=(_sha(3), _sha(2)),
            prunable_identities=(_sha(2), _sha(1)),
        ),
    )
    with pytest.raises(DatabaseHistoryReplacementAuthorizationError, match="overlap"):
        authorize_database_history_replacement(evidence=forged, prewrite=_prewrite(evidence))


def test_authorization_rejects_incomplete_partition() -> None:
    evidence = _evidence()
    forged = replace(
        evidence,
        plan=replace(
            evidence.plan,
            retained_identities=(_sha(3),),
            prunable_identities=(_sha(1),),
        ),
    )
    with pytest.raises(DatabaseHistoryReplacementAuthorizationError, match="does not match"):
        authorize_database_history_replacement(evidence=forged, prewrite=_prewrite(evidence))


def test_authorization_rejects_forged_plan_metadata() -> None:
    evidence = _evidence()
    forged = replace(evidence, plan=replace(evidence.plan, retention_days=8))
    with pytest.raises(DatabaseHistoryReplacementAuthorizationError, match="plan is inconsistent"):
        authorize_database_history_replacement(evidence=forged, prewrite=_prewrite(evidence))


def test_authorization_rejects_non_database_evidence() -> None:
    evidence = replace(_evidence(), branch="logs")
    with pytest.raises(DatabaseHistoryReplacementAuthorizationError, match="database branch"):
        authorize_database_history_replacement(evidence=evidence, prewrite=_prewrite(evidence))
