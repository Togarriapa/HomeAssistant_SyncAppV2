from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from ha_syncapp.administrative_retry_request import (
    AdministrativeRetryRequest,
    apply_administrative_retry_request,
    load_administrative_retry_receipt,
)
from ha_syncapp.candidate_backup_execution import CandidateBackupRuntimeEvidence
from ha_syncapp.candidate_dependency_execution import CandidateDependencyRuntimeEvidence
from ha_syncapp.candidate_fetch_stage_execution import CandidateFetchStageRuntimeEvidence
from ha_syncapp.candidate_integrity_execution import CandidateIntegrityRuntimeEvidence
from ha_syncapp.candidate_risk_execution import CandidateRiskRuntimeEvidence
from ha_syncapp.candidate_semantic_execution import CandidateSemanticRuntimeEvidence
from ha_syncapp.candidate_static_execution import CandidateStaticRuntimeEvidence
from ha_syncapp.retrigger_runtime_status import (
    MAX_RECOVERY_EVIDENCE_ROWS,
    RetriggerRuntimeStatusError,
    collect_retrigger_runtime_inventory,
    render_administrative_retry_runtime_status,
    render_candidate_backup_runtime_status,
    render_candidate_dependency_runtime_status,
    render_candidate_fetch_stage_runtime_status,
    render_candidate_integrity_runtime_status,
    render_candidate_risk_runtime_status,
    render_candidate_semantic_runtime_status,
    render_candidate_static_runtime_status,
    render_deployment_rollback_runtime_status,
    render_retrigger_runtime_status,
)
from ha_syncapp.state import (
    AdministrativeRetryRuntimeEvidence,
    DeploymentRollbackRuntimeEvidence,
    RecoveryWorkEvidence,
    StateStore,
)

NOW = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
STATUSES = {"pending", "running", "retry", "blocked", "succeeded"}
REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"
SECOND_REQUEST_ID = "123e4567-e89b-42d3-a456-426614174001"


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


def test_collects_administrative_retry_outcomes_without_request_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        work_key = "private-admin-work-key-sentinel"
        store.enqueue_work("candidate", work_key, now=NOW - timedelta(minutes=3))
        claimed = store.claim_work_kind("candidate", now=NOW - timedelta(minutes=2))
        assert claimed is not None
        store.fail_work(claimed, transient=False, now=NOW - timedelta(minutes=1))
        apply_administrative_retry_request(
            store,
            AdministrativeRetryRequest(REQUEST_ID, "candidate", work_key),
            now=NOW - timedelta(seconds=30),
        )
        apply_administrative_retry_request(
            store,
            AdministrativeRetryRequest(
                SECOND_REQUEST_ID,
                "privatekind",
                "private-missing-key-sentinel",
            ),
            now=NOW,
        )
        first_receipt = load_administrative_retry_receipt(store, REQUEST_ID)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    assert inventory.analysis["recovery"]["administrative_retry_requests"] == {
        "total": 2,
        "outcomes": {"rejected": 1, "retried": 1},
        "latest_processed_at": NOW.isoformat(),
    }
    encoded = json.dumps(inventory.analysis["recovery"], sort_keys=True)
    assert first_receipt is not None
    for secret in (
        REQUEST_ID,
        SECOND_REQUEST_ID,
        "private-admin-work-key-sentinel",
        "private-missing-key-sentinel",
        "privatekind",
        first_receipt.identity_sha256,
        first_receipt.record_sha256,
    ):
        assert secret not in encoded


def test_empty_runtime_inventory_has_explicit_administrative_retry_counts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    assert inventory.analysis["recovery"]["administrative_retry_requests"] == {
        "total": 0,
        "outcomes": {"rejected": 0, "retried": 0},
        "latest_processed_at": None,
    }


def test_candidate_apply_recovery_is_visible_without_deployment_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-identity"
        item = store.enqueue_work("candidate_apply", deployment_id, now=NOW)
        claimed = store.claim_work_kind(item.work_kind, now=NOW)
        assert claimed is not None
        store.fail_work(claimed, transient=True, now=NOW)

        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_apply"] == {
        "kind": "candidate_apply",
        "total": 1,
        "statuses": {
            "blocked": 0,
            "pending": 0,
            "retry": 1,
            "running": 0,
            "succeeded": 0,
        },
        "attempts": {"maximum": 1, "total": 1},
        "ready": 0,
        "backoff": {
            "scheduled": 1,
            "next_attempt_at": "2026-09-13T01:01:00+00:00",
        },
    }
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_apply_execution_is_visible_without_deployment_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-identity"
        store.enqueue_work("candidate_apply_execute", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_apply_execute"]["statuses"]["pending"] == 1
    assert kinds["candidate_apply_execute"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_entity_observation_is_visible_without_deployment_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-entity-observation"
        store.enqueue_work("candidate_observe_entities", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_observe_entities"]["statuses"]["pending"] == 1
    assert kinds["candidate_observe_entities"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_automation_observation_is_visible_without_deployment_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-automation-observation"
        store.enqueue_work("candidate_observe_automation_scripts", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_observe_automation_scripts"]["statuses"]["pending"] == 1
    assert kinds["candidate_observe_automation_scripts"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_assertion_observation_is_visible_without_deployment_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-assertion-observation"
        store.enqueue_work("candidate_observe_assertions", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_observe_assertions"]["statuses"]["pending"] == 1
    assert kinds["candidate_observe_assertions"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_finalization_is_visible_without_deployment_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-finalization"
        store.enqueue_work("candidate_finalize", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_finalize"]["statuses"]["pending"] == 1
    assert kinds["candidate_finalize"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_promotion_is_visible_without_deployment_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-promotion"
        store.enqueue_work("candidate_promote", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_promote"]["statuses"]["pending"] == 1
    assert kinds["candidate_promote"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_rollback_is_visible_without_deployment_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-rollback"
        store.enqueue_work("candidate_rollback", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_rollback"]["statuses"]["pending"] == 1
    assert kinds["candidate_rollback"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_restart_is_visible_without_deployment_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-identity"
        store.enqueue_work("candidate_restart", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_restart"]["statuses"]["pending"] == 1
    assert kinds["candidate_restart"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_core_observation_is_visible_without_deployment_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-identity"
        store.enqueue_work("candidate_observe", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_observe"]["statuses"]["pending"] == 1
    assert kinds["candidate_observe"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_supervisor_observation_is_visible_without_deployment_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-identity"
        store.enqueue_work("candidate_observe_supervisor", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_observe_supervisor"]["statuses"]["pending"] == 1
    assert kinds["candidate_observe_supervisor"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_integration_observation_is_visible_without_deployment_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-identity"
        store.enqueue_work("candidate_observe_integrations", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_observe_integrations"]["statuses"]["pending"] == 1
    assert kinds["candidate_observe_integrations"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_startup_error_observation_is_visible_without_deployment_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-identity"
        store.enqueue_work("candidate_observe_startup_errors", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_observe_startup_errors"]["statuses"]["pending"] == 1
    assert kinds["candidate_observe_startup_errors"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


def test_candidate_resource_observation_is_visible_without_deployment_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        deployment_id = "deployment-secret-identity"
        store.enqueue_work("candidate_observe_resources", deployment_id, now=NOW)
        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["candidate_observe_resources"]["statuses"]["pending"] == 1
    assert kinds["candidate_observe_resources"]["ready"] == 1
    assert deployment_id not in json.dumps(recovery, sort_keys=True)


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
        "administrative_retry_requests": {
            "total": 0,
            "outcomes": {"rejected": 0, "retried": 0},
            "latest_processed_at": None,
        },
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
        "candidate_integrity": {
            "total": 0,
            "phases": {"completed": 0, "planned": 0},
            "changed_paths": 0,
            "latest_updated_at": None,
        },
        "candidate_dependencies": {
            "total": 0,
            "phases": {"completed": 0, "planned": 0},
            "references": 0,
            "latest_updated_at": None,
        },
        "candidate_risk": {
            "total": 0,
            "phases": {"completed": 0, "planned": 0},
            "levels": {"low": 0, "medium": 0, "high": 0, "critical": 0},
            "affected_entities": 0,
            "latest_updated_at": None,
        },
        "candidate_static": {
            "total": 0,
            "phases": {"completed": 0, "planned": 0},
            "outcomes": {"valid": 0, "invalid": 0},
            "invalid_paths": 0,
            "unvalidated_paths": 0,
            "latest_updated_at": None,
        },
        "candidate_semantic": {
            "total": 0,
            "phases": {"blocked": 0, "completed": 0, "planned": 0},
            "outcomes": {"blocked": 0, "succeeded": 0},
            "latest_updated_at": None,
        },
        "candidate_backup": {
            "total": 0,
            "phases": {
                "blocked": 0,
                "completed": 0,
                "mutation_started": 0,
                "planned": 0,
                "uncertain": 0,
            },
            "outcomes": {"blocked": 0, "succeeded": 0},
            "mutation_started": 0,
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


def test_candidate_integrity_status_exposes_only_bounded_aggregate_state() -> None:
    evidence = (
        CandidateIntegrityRuntimeEvidence(
            phase="planned",
            changed_count=None,
            planned_at=NOW - timedelta(minutes=2),
            completed_at=None,
        ),
        CandidateIntegrityRuntimeEvidence(
            phase="completed",
            changed_count=3,
            planned_at=NOW - timedelta(minutes=3),
            completed_at=NOW - timedelta(minutes=1),
        ),
    )

    status = render_candidate_integrity_runtime_status(evidence, reference_time=NOW)

    assert status == {
        "total": 2,
        "phases": {"completed": 1, "planned": 1},
        "changed_paths": 3,
        "latest_updated_at": "2026-09-13T00:59:00+00:00",
    }


def test_candidate_dependency_status_exposes_only_bounded_aggregate_state() -> None:
    evidence = (
        CandidateDependencyRuntimeEvidence("planned", None, NOW - timedelta(seconds=2), None),
        CandidateDependencyRuntimeEvidence(
            "completed", 3, NOW - timedelta(seconds=3), NOW - timedelta(seconds=1)
        ),
    )

    status = render_candidate_dependency_runtime_status(evidence, reference_time=NOW)

    assert status == {
        "total": 2,
        "phases": {"completed": 1, "planned": 1},
        "references": 3,
        "latest_updated_at": (NOW - timedelta(seconds=1)).isoformat(),
    }
    encoded = json.dumps(status, sort_keys=True)
    assert "candidate_sha" not in encoded
    assert "repository" not in encoded


def test_candidate_risk_status_exposes_only_sanitized_aggregate_state() -> None:
    evidence = (
        CandidateRiskRuntimeEvidence("completed", "high", 3, NOW, NOW),
        CandidateRiskRuntimeEvidence("planned", None, None, NOW, None),
    )
    status = render_candidate_risk_runtime_status(evidence, reference_time=NOW)
    assert status["phases"] == {"completed": 1, "planned": 1}
    assert status["levels"]["high"] == 1
    assert status["affected_entities"] == 3
    encoded = json.dumps(status, sort_keys=True)
    assert "candidate_sha" not in encoded
    assert "repository" not in encoded


def test_candidate_static_status_exposes_only_sanitized_aggregate_state() -> None:
    evidence = (
        CandidateStaticRuntimeEvidence(
            "completed", False, 2, 1, NOW - timedelta(minutes=2), NOW - timedelta(minutes=1)
        ),
    )
    status = render_candidate_static_runtime_status(evidence, reference_time=NOW)
    assert status["outcomes"] == {"valid": 0, "invalid": 1}
    assert status["invalid_paths"] == 2
    assert status["unvalidated_paths"] == 1
    encoded = json.dumps(status, sort_keys=True)
    assert "candidate_sha" not in encoded
    assert "repository" not in encoded


def test_candidate_semantic_status_exposes_only_sanitized_aggregate_state() -> None:
    evidence = (
        CandidateSemanticRuntimeEvidence(
            "completed", True, NOW - timedelta(minutes=2), NOW - timedelta(minutes=1)
        ),
        CandidateSemanticRuntimeEvidence(
            "blocked", False, NOW - timedelta(minutes=3), NOW - timedelta(minutes=2)
        ),
        CandidateSemanticRuntimeEvidence("planned", None, NOW, None),
    )
    status = render_candidate_semantic_runtime_status(evidence, reference_time=NOW)
    assert status["phases"] == {"blocked": 1, "completed": 1, "planned": 1}
    assert status["outcomes"] == {"blocked": 1, "succeeded": 1}
    encoded = json.dumps(status, sort_keys=True)
    assert "candidate_sha" not in encoded
    assert "repository" not in encoded


def test_candidate_backup_status_exposes_only_sanitized_aggregate_state() -> None:
    evidence = (
        CandidateBackupRuntimeEvidence(
            "completed",
            True,
            True,
            NOW - timedelta(minutes=3),
            NOW - timedelta(minutes=2),
            NOW - timedelta(minutes=1),
        ),
        CandidateBackupRuntimeEvidence(
            "uncertain", True, None, NOW - timedelta(minutes=2), NOW - timedelta(minutes=1), None
        ),
        CandidateBackupRuntimeEvidence("planned", False, None, NOW, None, None),
    )
    status = render_candidate_backup_runtime_status(evidence, reference_time=NOW)
    assert status["phases"] == {
        "blocked": 0,
        "completed": 1,
        "mutation_started": 0,
        "planned": 1,
        "uncertain": 1,
    }
    assert status["outcomes"] == {"blocked": 0, "succeeded": 1}
    assert status["mutation_started"] == 2
    encoded = json.dumps(status, sort_keys=True)
    assert "candidate_sha" not in encoded
    assert "backup_slug" not in encoded


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


def test_render_administrative_retry_outcomes_deterministically() -> None:
    evidence = (
        AdministrativeRetryRuntimeEvidence("rejected", NOW - timedelta(minutes=2)),
        AdministrativeRetryRuntimeEvidence("retried", NOW - timedelta(minutes=1)),
        AdministrativeRetryRuntimeEvidence("rejected", NOW),
    )

    assert render_administrative_retry_runtime_status(evidence, reference_time=NOW) == {
        "total": 3,
        "outcomes": {"rejected": 2, "retried": 1},
        "latest_processed_at": NOW.isoformat(),
    }


@pytest.mark.parametrize(
    "evidence",
    [
        object(),
        AdministrativeRetryRuntimeEvidence("unknown", NOW),
        AdministrativeRetryRuntimeEvidence("retried", NOW.replace(tzinfo=None)),
        AdministrativeRetryRuntimeEvidence("retried", NOW + timedelta(seconds=1)),
        AdministrativeRetryRuntimeEvidence(
            "retried",
            NOW.astimezone(timezone(timedelta(hours=1))),
        ),
    ],
)
def test_render_administrative_retry_rejects_malformed_evidence(evidence: object) -> None:
    with pytest.raises(RetriggerRuntimeStatusError, match="evidence is invalid"):
        render_administrative_retry_runtime_status((evidence,), reference_time=NOW)


def test_render_administrative_retry_bounds_evidence_rows() -> None:
    evidence = tuple(
        AdministrativeRetryRuntimeEvidence("rejected", NOW)
        for _ in range(MAX_RECOVERY_EVIDENCE_ROWS + 1)
    )
    with pytest.raises(RetriggerRuntimeStatusError, match="limit"):
        render_administrative_retry_runtime_status(evidence, reference_time=NOW)


def test_documentation_defines_identity_free_administrative_retry_status() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime_docs = (root / "docs" / "retrigger-runtime-status.md").read_text()
    operator_docs = (root / "syncapp" / "DOCS.md").read_text()

    for required in (
        "`administrative_retry_requests`",
        "`retried` and `rejected`",
        "latest processed UTC timestamp",
        "request IDs",
        "identity digests",
        "read-only",
    ):
        assert required in runtime_docs
    assert "`analysis/recovery.json`" in operator_docs
    assert "administrative retry outcome aggregates" in " ".join(operator_docs.split())
