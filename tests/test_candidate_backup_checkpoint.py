from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.candidate_backup_checkpoint import (
    CandidateBackupCheckpoint,
    CandidateBackupCheckpointError,
    candidate_backup_deployment_id,
)

NOW = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
CANDIDATE = "b" * 40
DEPLOYMENT = "0e9eef73-915f-5acd-b90f-1b828aab55ac"


def _plan() -> CandidateBackupCheckpoint:
    return CandidateBackupCheckpoint.plan(
        candidate_sha=CANDIDATE,
        orchestration_sha256="1" * 64,
        fetch_stage_sha256="2" * 64,
        integrity_sha256="3" * 64,
        dependency_sha256="4" * 64,
        risk_sha256="5" * 64,
        static_sha256="6" * 64,
        semantic_sha256="7" * 64,
        target="Owner/Home",
        repository_id=123,
        baseline_sha="a" * 40,
        stage_manifest_sha256="8" * 64,
        runtime_sha256="9" * 64,
        risk_level="high",
        core_version="2026.9.1",
        planned_at=NOW,
    )


def _evidence(slug: str = "backup-123") -> CandidateBackupEvidence:
    return CandidateBackupEvidence(
        target="Owner/Home",
        repository_id=123,
        baseline_sha="a" * 40,
        candidate_sha=CANDIDATE,
        stage_manifest_sha256="8" * 64,
        runtime_sha256="9" * 64,
        risk_level="high",
        core_version="2026.9.1",
        backup_slug=slug,
    )


def test_backup_checkpoint_journals_before_mutation_and_round_trips_success() -> None:
    plan = _plan()
    assert plan.deployment_id == DEPLOYMENT
    assert plan.request_name == f"SyncApp candidate {CANDIDATE[:12]} {DEPLOYMENT}"
    started = plan.start(started_at=NOW + timedelta(seconds=1))
    assert started.phase == "mutation_started"
    completed = started.complete(_evidence(), completed_at=NOW + timedelta(seconds=2))
    restored = CandidateBackupCheckpoint.from_database_row(completed.database_values())
    assert restored == completed
    assert restored.evidence() == _evidence()


def test_started_checkpoint_cannot_authorize_a_second_mutation() -> None:
    started = _plan().start(started_at=NOW + timedelta(seconds=1))
    with pytest.raises(CandidateBackupCheckpointError):
        started.start(started_at=NOW + timedelta(seconds=2))


def test_uncertain_outcome_round_trips_without_backup_evidence() -> None:
    uncertain = _plan().start(started_at=NOW + timedelta(seconds=1)).mark_uncertain()
    restored = CandidateBackupCheckpoint.from_database_row(uncertain.database_values())
    assert restored.phase == "uncertain"
    with pytest.raises(CandidateBackupCheckpointError):
        restored.evidence()
    with pytest.raises(CandidateBackupCheckpointError):
        restored.start(started_at=NOW + timedelta(seconds=2))


def test_deterministic_failure_blocks_before_mutation() -> None:
    blocked = _plan().block(completed_at=NOW + timedelta(seconds=1))
    restored = CandidateBackupCheckpoint.from_database_row(blocked.database_values())
    assert restored.phase == "blocked"
    with pytest.raises(CandidateBackupCheckpointError):
        restored.evidence()


@pytest.mark.parametrize(
    "changes",
    [
        {"target": "Owner/Other"},
        {"repository_id": 124},
        {"candidate_sha": "c" * 40},
        {"baseline_sha": "c" * 40},
        {"stage_manifest_sha256": "c" * 64},
        {"runtime_sha256": "c" * 64},
        {"risk_level": "low"},
        {"core_version": "2026.9.2"},
    ],
)
def test_backup_checkpoint_rejects_rebound_evidence(changes) -> None:
    started = _plan().start(started_at=NOW + timedelta(seconds=1))
    with pytest.raises(CandidateBackupCheckpointError):
        started.complete(replace(_evidence(), **changes), completed_at=NOW + timedelta(seconds=2))


def test_deployment_identity_is_canonical_and_candidate_bound() -> None:
    assert candidate_backup_deployment_id("Owner/Home", 123, CANDIDATE) == DEPLOYMENT
    assert candidate_backup_deployment_id("Owner/Home", 123, "c" * 40) != DEPLOYMENT
    with pytest.raises(CandidateBackupCheckpointError):
        candidate_backup_deployment_id("Owner/Home", True, CANDIDATE)


def test_corrupt_checkpoint_is_rejected_without_disclosure() -> None:
    row = list(_plan().database_values())
    row[17] = "secret-sentinel"
    with pytest.raises(CandidateBackupCheckpointError) as caught:
        CandidateBackupCheckpoint.from_database_row(tuple(row))
    assert "secret-sentinel" not in str(caught.value)
