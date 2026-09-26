from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.candidate_fetch_stage_execution import CandidateFetchStageRuntimeEvidence
from ha_syncapp.retrigger_runtime_status import (
    MAX_RECOVERY_EVIDENCE_ROWS,
    RetriggerRuntimeStatusError,
    collect_retrigger_runtime_inventory,
    render_candidate_fetch_stage_runtime_status,
    render_deployment_rollback_runtime_status,
    render_retrigger_runtime_status,
)
from ha_syncapp.state import (
    DeploymentRollbackRuntimeEvidence,
    RecoveryWorkEvidence,
    StateStore,
)

NOW = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
STATUSES = {"pending", "running", "retry", "blocked", "succeeded"}


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_collects_aggregate_status_attempt_and_backoff_without_work_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.enqueue_work("candidate", "candidate-secret-sha", now=NOW)
        running = store.enqueue_work("local_sync", "/secret/source/path", now=NOW)
        running = store.claim_work_kind(running.work_kind, now=NOW)
        assert running is not None

        retry = store.enqueue_work("database", "repo:/secret/db", now=NOW)
        retry = store.claim_work_kind(retry.work_kind, now=NOW)
        assert retry is not None
        store.fail_work(retry, transient=True, now=NOW)

        blocked = store.enqueue_work("logs", "token-like-secret", now=NOW)
        blocked = store.claim_work_kind(blocked.work_kind, now=NOW)
        assert blocked is not None
        store.fail_work(blocked, transient=False, now=NOW)

        succeeded = store.enqueue_work("runtime", "runtime-secret", now=NOW)
        succeeded = store.claim_work_kind(succeeded.work_kind, now=NOW)
        assert succeeded is not None
        store.complete_work(succeeded, now=NOW)

        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
        second = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    assert recovery == second.analysis["recovery"]
    assert recovery["total"] == 5
    assert set(recovery["statuses"]) == STATUSES
    assert recovery["statuses"] == {
        "blocked": 1,
        "pending": 1,
        "retry": 1,
        "running": 1,
        "succeeded": 1,
    }
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["database"]["backoff"] == {
        "scheduled": 1,
        "next_attempt_at": "2026-09-13T01:01:00+00:00",
    }
    assert kinds["database"]["attempts"] == {"maximum": 1, "total": 1}
    encoded = json.dumps(recovery, sort_keys=True)
    for secret in (
        "candidate-secret-sha",
        "/secret/source/path",
        "repo:/secret/db",
        "token-like-secret",
        "runtime-secret",
    ):
        assert secret not in encoded


def test_empty_status_is_explicit_and_deterministic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        first = collect_retrigger_runtime_inventory(store, reference_time=NOW)
        second = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    assert first == second
    assert first.analysis["recovery"] == {
        "reference_time": "2026-09-13T01:00:00+00:00",
        "total": 0,
        "statuses": {status: 0 for status in sorted(STATUSES)},
        "attempts": {"maximum": 0, "total": 0},
        "ready": 0,
        "backoff": {"scheduled": 0, "next_attempt_at": None},
        "kinds": [],
        "deployment_rollback": {
            "total": 0,
            "phases": {
                "blocked": 0,
                "completed": 0,
                "observing": 0,
                "planned": 0,
                "restore_acknowledged": 0,
                "restore_started": 0,
                "uncertain": 0,
            },
            "reconciliation": {
                "ambiguous": 0,
                "in_progress": 0,
                "none": 0,
                "not_started": 0,
                "restored": 0,
            },
            "block_reasons": {
                "ambiguous": 0,
                "backup_invalid": 0,
                "invalid_authority": 0,
                "none": 0,
                "repository_divergence": 0,
                "restore_rejected": 0,
            },
            "attempts": {"maximum": 0, "total": 0},
            "latest_updated_at": None,
        },
        "candidate_fetch_stage": {
            "total": 0,
            "phases": {"completed": 0, "planned": 0},
            "staged": {"entries": 0, "bytes": 0},
            "latest_updated_at": None,
        },
    }


def test_fetch_stage_status_exposes_only_bounded_aggregate_state() -> None:
    evidence = (
        CandidateFetchStageRuntimeEvidence(
            phase="planned",
            entry_count=None,
            total_bytes=None,
            planned_at=NOW - timedelta(minutes=2),
            completed_at=None,
        ),
        CandidateFetchStageRuntimeEvidence(
            phase="completed",
            entry_count=3,
            total_bytes=2048,
            planned_at=NOW - timedelta(minutes=3),
            completed_at=NOW - timedelta(minutes=1),
        ),
    )

    status = render_candidate_fetch_stage_runtime_status(evidence, reference_time=NOW)

    assert status == {
        "total": 2,
        "phases": {"completed": 1, "planned": 1},
        "staged": {"entries": 3, "bytes": 2048},
        "latest_updated_at": "2026-09-13T00:59:00+00:00",
    }
    encoded = json.dumps(status, sort_keys=True)
    assert "candidate_sha" not in encoded
    assert "repository" not in encoded


def test_rollback_runtime_status_exposes_only_bounded_aggregate_state() -> None:
    evidence = (
        DeploymentRollbackRuntimeEvidence(
            phase="observing",
            reconciliation_state="restored",
            block_reason="none",
            attempt_count=1,
            updated_at=NOW - timedelta(minutes=2),
        ),
        DeploymentRollbackRuntimeEvidence(
            phase="blocked",
            reconciliation_state="ambiguous",
            block_reason="ambiguous",
            attempt_count=2,
            updated_at=NOW - timedelta(minutes=1),
        ),
    )

    status = render_deployment_rollback_runtime_status(evidence, reference_time=NOW)

    assert status["total"] == 2
    assert status["phases"]["observing"] == 1
    assert status["phases"]["blocked"] == 1
    assert status["reconciliation"]["restored"] == 1
    assert status["reconciliation"]["ambiguous"] == 1
    assert status["block_reasons"]["ambiguous"] == 1
    assert status["attempts"] == {"maximum": 2, "total": 3}
    assert status["latest_updated_at"] == "2026-09-13T00:59:00+00:00"
    encoded = json.dumps(status, sort_keys=True)
    assert "deployment_id" not in encoded
    assert "backup_slug" not in encoded
    assert "candidate" not in encoded


def test_collect_includes_sanitized_rollback_runtime_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    values = (
        "secret-deployment-id",
        "Owner/Private-Home",
        123,
        "a" * 40,
        "b" * 40,
        "secret-backup-slug",
        "c" * 64,
        "d" * 64,
        "e" * 64,
        "blocked",
        "ambiguous",
        "ambiguous",
        2,
        None,
        (NOW - timedelta(minutes=2)).isoformat(),
        (NOW - timedelta(minutes=1)).isoformat(),
        "f" * 64,
    )
    store._connection.execute(
        "INSERT INTO deployment_rollback VALUES (" + ",".join("?" for _ in values) + ")",
        values,
    )
    store._connection.commit()
    try:
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    rollback = inventory.analysis["recovery"]["deployment_rollback"]
    assert rollback["total"] == 1
    assert rollback["phases"]["blocked"] == 1
    encoded = json.dumps(inventory.analysis, sort_keys=True)
    assert "secret-deployment-id" not in encoded
    assert "secret-backup-slug" not in encoded
    assert "Owner/Private-Home" not in encoded


def test_collection_is_read_only_for_pending_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        original = store.enqueue_work("candidate", "unchanged", now=NOW)
        collect_retrigger_runtime_inventory(store, reference_time=NOW)
        observed = store.enqueue_work("candidate", "unchanged", now=NOW + timedelta(hours=1))
    finally:
        store.__exit__(None, None, None)

    assert observed == original


def _evidence(**changes: object) -> RecoveryWorkEvidence:
    base = RecoveryWorkEvidence(
        work_kind="candidate",
        status="retry",
        attempts=1,
        created_at=NOW - timedelta(minutes=2),
        updated_at=NOW - timedelta(minutes=1),
        next_attempt_at=NOW + timedelta(minutes=1),
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(work_kind="../secret"),
        _evidence(status="unknown"),
        _evidence(attempts=-1),
        _evidence(attempts=StateStore.MAX_WORK_ATTEMPTS + 1),
        _evidence(created_at=NOW.replace(tzinfo=None)),
        _evidence(updated_at=NOW - timedelta(hours=1)),
        _evidence(status="blocked", next_attempt_at=NOW),
        _evidence(status="retry", next_attempt_at=None),
    ],
)
def test_render_rejects_malformed_evidence(evidence: RecoveryWorkEvidence) -> None:
    with pytest.raises(RetriggerRuntimeStatusError, match="evidence is invalid"):
        render_retrigger_runtime_status((evidence,), reference_time=NOW)


def test_render_rejects_invalid_reference_time() -> None:
    with pytest.raises(RetriggerRuntimeStatusError, match="reference time is invalid"):
        render_retrigger_runtime_status((), reference_time=NOW.replace(tzinfo=None))


def test_render_bounds_evidence_rows() -> None:
    evidence = tuple(_evidence() for _ in range(MAX_RECOVERY_EVIDENCE_ROWS + 1))
    with pytest.raises(RetriggerRuntimeStatusError, match="limit"):
        render_retrigger_runtime_status(evidence, reference_time=NOW)
