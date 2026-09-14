from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest
from ha_syncapp.live_apply_intent import LiveApplyIntent
from ha_syncapp.live_apply_plan import LiveApplyOperation, LiveApplyPlan
from ha_syncapp.live_apply_progress import (
    LiveApplyProgress,
    LiveApplyProgressError,
    start_live_apply_progress,
    transition_live_apply_progress,
)


def _progress(phase: str = "mutation_started") -> LiveApplyProgress:
    return LiveApplyProgress.create(
        deployment_id="deploy-12345678",
        intent_record_sha256="a" * 64,
        operations_sha256="b" * 64,
        operation_index=2,
        operation_path_sha256="c" * 64,
        phase=phase,
    )


def _operation(path: str) -> LiveApplyOperation:
    return LiveApplyOperation(
        path=path,
        status="modified",
        baseline_mode="100644",
        baseline_object_id="1" * 40,
        candidate_mode="100644",
        candidate_object_id="2" * 40,
        staged_size=7,
        staged_sha256="3" * 64,
    )


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


def _bound_chain() -> tuple[LiveApplyIntent, LiveApplyPlan]:
    operations = (_operation("automations.yaml"), _operation("scripts.yaml"))
    operations_sha256 = hashlib.sha256(
        json.dumps(
            tuple(_operation_tuple(operation) for operation in operations),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()

    intent = object.__new__(LiveApplyIntent)
    for name, value in {
        "deployment_id": "deploy-12345678",
        "target": "owner/repository",
        "repository_id": 42,
        "baseline_sha": "4" * 40,
        "candidate_sha": "5" * 40,
        "stage_manifest_sha256": "6" * 64,
        "backup_slug": "backup-123",
        "homeassistant_root": "/homeassistant",
        "operations_sha256": operations_sha256,
    }.items():
        object.__setattr__(intent, name, value)

    plan = object.__new__(LiveApplyPlan)
    for name, value in {
        "deployment_id": intent.deployment_id,
        "target": intent.target,
        "repository_id": intent.repository_id,
        "baseline_sha": intent.baseline_sha,
        "candidate_sha": intent.candidate_sha,
        "stage_manifest_sha256": intent.stage_manifest_sha256,
        "operations": operations,
    }.items():
        object.__setattr__(plan, name, value)
    return intent, plan


def test_progress_is_immutable_and_content_free() -> None:
    progress = _progress()

    assert progress.phase == "mutation_started"
    assert progress.operation_index == 2
    assert not hasattr(progress, "path")
    assert not hasattr(progress, "content")

    with pytest.raises(FrozenInstanceError):
        progress.phase = "mutation_verified"  # type: ignore[misc]


def test_exact_transition_replay_is_idempotent() -> None:
    current = _progress()

    assert transition_live_apply_progress(current, "mutation_started") is current


def test_started_can_be_verified_or_blocked() -> None:
    current = _progress()

    verified = transition_live_apply_progress(current, "mutation_verified")
    blocked = transition_live_apply_progress(current, "blocked")

    assert verified.phase == "mutation_verified"
    assert blocked.phase == "blocked"
    assert verified.deployment_id == current.deployment_id
    assert verified.intent_record_sha256 == current.intent_record_sha256
    assert verified.operations_sha256 == current.operations_sha256
    assert verified.operation_index == current.operation_index
    assert verified.operation_path_sha256 == current.operation_path_sha256


def test_terminal_progress_cannot_regress_or_change_terminal_state() -> None:
    for phase in ("mutation_verified", "blocked"):
        current = _progress(phase)
        assert transition_live_apply_progress(current, phase) is current
        for destination in ("mutation_started", "mutation_verified", "blocked"):
            if destination == phase:
                continue
            with pytest.raises(LiveApplyProgressError, match="transition is not permitted"):
                transition_live_apply_progress(current, destination)


def test_invalid_progress_identity_and_phase_fail_closed() -> None:
    invalid_values = [
        {"deployment_id": ""},
        {"intent_record_sha256": "not-a-hash"},
        {"operations_sha256": "d" * 63},
        {"operation_index": -1},
        {"operation_path_sha256": "e" * 65},
        {"phase": "succeeded"},
    ]

    baseline = {
        "deployment_id": "deploy-12345678",
        "intent_record_sha256": "a" * 64,
        "operations_sha256": "b" * 64,
        "operation_index": 0,
        "operation_path_sha256": "c" * 64,
        "phase": "mutation_started",
    }
    for override in invalid_values:
        values = baseline | override
        with pytest.raises(LiveApplyProgressError):
            LiveApplyProgress.create(**values)


def test_transition_rejects_invalid_runtime_objects_and_phase() -> None:
    with pytest.raises(LiveApplyProgressError, match="progress evidence is invalid"):
        transition_live_apply_progress(object(), "mutation_verified")  # type: ignore[arg-type]

    with pytest.raises(LiveApplyProgressError, match="phase is invalid"):
        transition_live_apply_progress(_progress(), "unknown")


def test_start_progress_is_bound_to_exact_intent_plan_and_operation() -> None:
    intent, plan = _bound_chain()

    progress = start_live_apply_progress(intent, "a" * 64, plan, operation_index=1)

    assert progress.deployment_id == intent.deployment_id
    assert progress.intent_record_sha256 == "a" * 64
    assert progress.operations_sha256 == intent.operations_sha256
    assert progress.operation_index == 1
    assert progress.operation_path_sha256 == hashlib.sha256(b"scripts.yaml").hexdigest()
    assert progress.phase == "mutation_started"


def test_start_progress_rejects_plan_drift_reorder_and_invalid_index() -> None:
    intent, plan = _bound_chain()

    drifted = object.__new__(LiveApplyPlan)
    for name in (
        "deployment_id",
        "target",
        "repository_id",
        "baseline_sha",
        "candidate_sha",
        "stage_manifest_sha256",
    ):
        object.__setattr__(drifted, name, getattr(plan, name))
    object.__setattr__(drifted, "operations", tuple(reversed(plan.operations)))

    with pytest.raises(LiveApplyProgressError, match="operations binding"):
        start_live_apply_progress(intent, "a" * 64, drifted, operation_index=0)
    with pytest.raises(LiveApplyProgressError, match="operation index"):
        start_live_apply_progress(intent, "a" * 64, plan, operation_index=2)


def test_start_progress_rejects_cross_deployment_or_bad_record_binding() -> None:
    intent, plan = _bound_chain()
    object.__setattr__(plan, "candidate_sha", "9" * 40)

    with pytest.raises(LiveApplyProgressError, match="plan binding"):
        start_live_apply_progress(intent, "a" * 64, plan, operation_index=0)

    _, valid_plan = _bound_chain()
    with pytest.raises(LiveApplyProgressError, match="intent record binding"):
        start_live_apply_progress(intent, "not-a-hash", valid_plan, operation_index=0)
