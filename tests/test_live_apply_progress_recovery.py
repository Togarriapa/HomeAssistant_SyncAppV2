from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.live_apply_plan import LiveApplyOperation, LiveApplyPlan
from ha_syncapp.live_apply_progress import LiveApplyProgress, transition_live_apply_progress
from ha_syncapp.live_apply_progress_store import (
    discover_live_apply_recovery,
    record_live_apply_progress,
)
from ha_syncapp.prepared_deployment import PreparedDeployment
from ha_syncapp.state import StateStore


def _prepared() -> PreparedDeployment:
    evidence = CandidateBackupEvidence(
        target="owner/private-repo",
        repository_id=12345,
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
        stage_manifest_sha256="c" * 64,
        runtime_sha256="d" * 64,
        risk_level="high",
        core_version="2026.9.1",
        backup_slug="backup_123",
    )
    return PreparedDeployment(str(uuid4()), evidence, datetime.now(UTC))


def _operation(path: str, marker: str) -> LiveApplyOperation:
    return LiveApplyOperation(
        path=path,
        status="modified",
        baseline_mode="100644",
        baseline_object_id=marker * 40,
        candidate_mode="100644",
        candidate_object_id=("f" if marker != "f" else "e") * 40,
        staged_size=7,
        staged_sha256=marker * 64,
    )


def _plan(prepared: PreparedDeployment) -> LiveApplyPlan:
    evidence = prepared.evidence
    plan = object.__new__(LiveApplyPlan)
    for name, value in {
        "deployment_id": prepared.deployment_id,
        "target": evidence.target,
        "repository_id": evidence.repository_id,
        "baseline_sha": evidence.baseline_sha,
        "candidate_sha": evidence.candidate_sha,
        "stage_manifest_sha256": evidence.stage_manifest_sha256,
        "operations": (
            _operation("automations.yaml", "1"),
            _operation("scripts.yaml", "2"),
        ),
    }.items():
        object.__setattr__(plan, name, value)
    return plan


def _operation_tuple(operation: LiveApplyOperation) -> tuple[object, ...]:
    return (
        operation.path,
        operation.status,
        operation.baseline_mode,
        operation.baseline_object_id,
        operation.candidate_mode,
        operation.candidate_object_id,
        operation.staged_size,
        operation.staged_sha256,
    )


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _seed(store: StateStore, prepared: PreparedDeployment, plan: LiveApplyPlan) -> tuple[str, str]:
    evidence = prepared.evidence
    store.bind_repository(evidence.target, evidence.repository_id)
    store.record_prepared_deployment(prepared.deployment_id, evidence)
    operations_sha256 = _digest(tuple(_operation_tuple(item) for item in plan.operations))
    recorded_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).isoformat()
    values: tuple[object, ...] = (
        prepared.deployment_id,
        evidence.target,
        evidence.repository_id,
        evidence.baseline_sha,
        evidence.candidate_sha,
        evidence.stage_manifest_sha256,
        evidence.backup_slug,
        "/homeassistant",
        operations_sha256,
        recorded_at,
    )
    record_sha256 = _digest(values)
    store._connection.execute(
        "INSERT INTO live_apply_intent (deployment_id, target, repository_id, baseline_sha, "
        "candidate_sha, stage_manifest_sha256, backup_slug, homeassistant_root, "
        "operations_sha256, recorded_at, record_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*values, record_sha256),
    )
    store._connection.commit()
    return operations_sha256, record_sha256


def _progress(
    prepared: PreparedDeployment,
    plan: LiveApplyPlan,
    operations_sha256: str,
    record_sha256: str,
    index: int,
) -> LiveApplyProgress:
    return LiveApplyProgress.create(
        deployment_id=prepared.deployment_id,
        intent_record_sha256=record_sha256,
        operations_sha256=operations_sha256,
        operation_index=index,
        operation_path_sha256=hashlib.sha256(
            plan.operations[index].path.encode("utf-8")
        ).hexdigest(),
        phase="mutation_started",
    )


def test_restart_discovery_requires_reconciliation_for_uncertain_mutation(tmp_path: Path) -> None:
    prepared = _prepared()
    plan = _plan(prepared)
    with StateStore(tmp_path) as store:
        operations_sha256, record_sha256 = _seed(store, prepared, plan)
        initial = discover_live_apply_recovery(store, plan)
        assert initial.action == "start_next"
        assert initial.operation_index == 0

        started = _progress(prepared, plan, operations_sha256, record_sha256, 0)
        record_live_apply_progress(store, started, plan=plan)

    with StateStore(tmp_path) as store:
        recovered = discover_live_apply_recovery(store, plan)
        assert recovered.action == "reconcile_uncertain"
        assert recovered.operation_index == 0
        assert recovered.operation_path_sha256 == started.operation_path_sha256


def test_recovery_advances_only_after_verified_predecessor_and_stops_on_block(
    tmp_path: Path,
) -> None:
    prepared = _prepared()
    plan = _plan(prepared)
    with StateStore(tmp_path) as store:
        operations_sha256, record_sha256 = _seed(store, prepared, plan)
        first = _progress(prepared, plan, operations_sha256, record_sha256, 0)
        record_live_apply_progress(store, first, plan=plan)
        record_live_apply_progress(
            store,
            transition_live_apply_progress(first, "mutation_verified"),
            plan=plan,
        )

        decision = discover_live_apply_recovery(store, plan)
        assert decision.action == "start_next"
        assert decision.operation_index == 1

        second = _progress(prepared, plan, operations_sha256, record_sha256, 1)
        record_live_apply_progress(store, second, plan=plan)
        record_live_apply_progress(
            store,
            transition_live_apply_progress(second, "blocked"),
            plan=plan,
        )
        blocked = discover_live_apply_recovery(store, plan)
        assert blocked.action == "blocked"
        assert blocked.operation_index == 1


def test_recovery_reports_complete_only_when_every_operation_is_verified(tmp_path: Path) -> None:
    prepared = _prepared()
    plan = _plan(prepared)
    with StateStore(tmp_path) as store:
        operations_sha256, record_sha256 = _seed(store, prepared, plan)
        for index in range(len(plan.operations)):
            started = _progress(prepared, plan, operations_sha256, record_sha256, index)
            record_live_apply_progress(store, started, plan=plan)
            record_live_apply_progress(
                store,
                transition_live_apply_progress(started, "mutation_verified"),
                plan=plan,
            )

        decision = discover_live_apply_recovery(store, plan)
        assert decision.action == "complete"
        assert decision.operation_index is None
        assert decision.operation_path_sha256 is None
