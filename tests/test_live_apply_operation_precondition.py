from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from ha_syncapp.live_apply_plan import LiveApplyOperation, LiveApplyPlan
from ha_syncapp.live_apply_preconditions import (
    LiveApplyPreconditionError,
    prove_live_apply_operation_precondition,
)


def _blob_id(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def _plan(path: str, data: bytes) -> LiveApplyPlan:
    plan = object.__new__(LiveApplyPlan)
    operation = LiveApplyOperation(
        path=path,
        status="modified",
        baseline_mode="100644",
        baseline_object_id=_blob_id(data),
        candidate_mode="100644",
        candidate_object_id="2" * 40,
        staged_size=9,
        staged_sha256="3" * 64,
    )
    for name, value in {
        "deployment_id": "deploy-12345678",
        "target": "owner/private-repo",
        "repository_id": 12345,
        "baseline_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "stage_manifest_sha256": "c" * 64,
        "operations": (operation,),
    }.items():
        object.__setattr__(plan, name, value)
    return plan


def test_single_operation_precondition_reproves_exact_live_path(tmp_path: Path) -> None:
    data = b"baseline\n"
    (tmp_path / "automations.yaml").write_bytes(data)
    plan = _plan("automations.yaml", data)

    evidence = prove_live_apply_operation_precondition(plan, tmp_path, operation_index=0)

    assert evidence.deployment_id == plan.deployment_id
    assert evidence.operation_index == 0
    assert evidence.path == "automations.yaml"
    assert evidence.root == str(tmp_path)
    assert evidence.baseline_object_id == plan.operations[0].baseline_object_id
    assert evidence.baseline_mode == "100644"


def test_single_operation_precondition_detects_toctou_drift(tmp_path: Path) -> None:
    original = b"baseline\n"
    target = tmp_path / "automations.yaml"
    target.write_bytes(original)
    plan = _plan("automations.yaml", original)
    target.write_bytes(b"changed after earlier proof\n")

    with pytest.raises(LiveApplyPreconditionError, match="precondition mismatch"):
        prove_live_apply_operation_precondition(plan, tmp_path, operation_index=0)


def test_single_operation_precondition_rejects_symlink_leaf(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_bytes(b"baseline\n")
    (tmp_path / "automations.yaml").symlink_to(outside)
    plan = _plan("automations.yaml", b"baseline\n")

    with pytest.raises(LiveApplyPreconditionError, match="unsafe"):
        prove_live_apply_operation_precondition(plan, tmp_path, operation_index=0)


def test_single_operation_precondition_rejects_invalid_index(tmp_path: Path) -> None:
    plan = _plan("automations.yaml", b"baseline\n")

    with pytest.raises(LiveApplyPreconditionError, match="operation index"):
        prove_live_apply_operation_precondition(plan, tmp_path, operation_index=1)
