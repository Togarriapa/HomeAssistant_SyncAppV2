from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from ha_syncapp.live_apply_progress import (
    LiveApplyProgress,
    LiveApplyProgressError,
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
