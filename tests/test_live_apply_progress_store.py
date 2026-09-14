from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.live_apply_progress import LiveApplyProgress, transition_live_apply_progress
from ha_syncapp.live_apply_progress_store import (
    discover_live_apply_progress,
    load_live_apply_progress,
    record_live_apply_progress,
)
from ha_syncapp.prepared_deployment import PreparedDeployment
from ha_syncapp.state import StateError, StateStore


def _backup() -> CandidateBackupEvidence:
    return CandidateBackupEvidence(
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


def _prepared() -> PreparedDeployment:
    return PreparedDeployment(str(uuid4()), _backup(), datetime.now(UTC))


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _prepare_store(store: StateStore, prepared: PreparedDeployment) -> tuple[str, str]:
    evidence = prepared.evidence
    store.bind_repository(evidence.target, evidence.repository_id)
    store.record_prepared_deployment(prepared.deployment_id, evidence)
    recorded_at = datetime(2026, 9, 14, 12, 0, tzinfo=UTC).isoformat()
    operations_sha256 = "e" * 64
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
    operations_sha256: str,
    intent_record_sha256: str,
    *,
    operation_index: int,
    path: str,
    phase: str = "mutation_started",
) -> LiveApplyProgress:
    return LiveApplyProgress.create(
        deployment_id=prepared.deployment_id,
        intent_record_sha256=intent_record_sha256,
        operations_sha256=operations_sha256,
        operation_index=operation_index,
        operation_path_sha256=hashlib.sha256(path.encode("utf-8")).hexdigest(),
        phase=phase,
    )


def test_started_progress_persists_idempotently_and_survives_restart(tmp_path: Path) -> None:
    prepared = _prepared()
    first_time = datetime(2026, 9, 14, 12, 1, tzinfo=UTC)
    later = datetime(2026, 9, 14, 12, 2, tzinfo=UTC)

    with StateStore(tmp_path) as store:
        operations_sha256, intent_record_sha256 = _prepare_store(store, prepared)
        progress = _progress(
            prepared,
            operations_sha256,
            intent_record_sha256,
            operation_index=0,
            path="automations.yaml",
        )
        first = record_live_apply_progress(store, progress, updated_at=first_time)
        replay = record_live_apply_progress(store, progress, updated_at=later)
        assert replay == first
        assert first.updated_at == first_time
        assert first.progress == progress

    with StateStore(tmp_path) as store:
        assert load_live_apply_progress(store, prepared.deployment_id, 0) == first
        assert discover_live_apply_progress(store, prepared.deployment_id) == (first,)


def test_next_operation_requires_verified_contiguous_predecessor(tmp_path: Path) -> None:
    prepared = _prepared()
    with StateStore(tmp_path) as store:
        operations_sha256, intent_record_sha256 = _prepare_store(store, prepared)
        first = _progress(
            prepared,
            operations_sha256,
            intent_record_sha256,
            operation_index=0,
            path="automations.yaml",
        )
        second = _progress(
            prepared,
            operations_sha256,
            intent_record_sha256,
            operation_index=1,
            path="scripts.yaml",
        )
        record_live_apply_progress(store, first)
        with pytest.raises(StateError, match="previous operation is not verified"):
            record_live_apply_progress(store, second)

        verified = transition_live_apply_progress(first, "mutation_verified")
        record_live_apply_progress(store, verified)
        stored_second = record_live_apply_progress(store, second)
        assert stored_second.progress == second

        skipped = _progress(
            prepared,
            operations_sha256,
            intent_record_sha256,
            operation_index=3,
            path="scenes.yaml",
        )
        with pytest.raises(StateError, match="operations must be contiguous"):
            record_live_apply_progress(store, skipped)


def test_progress_transition_is_monotonic_and_conflicts_fail_closed(tmp_path: Path) -> None:
    prepared = _prepared()
    with StateStore(tmp_path) as store:
        operations_sha256, intent_record_sha256 = _prepare_store(store, prepared)
        started = _progress(
            prepared,
            operations_sha256,
            intent_record_sha256,
            operation_index=0,
            path="automations.yaml",
        )
        record_live_apply_progress(store, started)
        verified = transition_live_apply_progress(started, "mutation_verified")
        durable = record_live_apply_progress(store, verified)
        assert durable.progress.phase == "mutation_verified"

        with pytest.raises(StateError, match="transition is not permitted"):
            record_live_apply_progress(store, started)

        conflicting = LiveApplyProgress.create(
            deployment_id=started.deployment_id,
            intent_record_sha256=started.intent_record_sha256,
            operations_sha256=started.operations_sha256,
            operation_index=started.operation_index,
            operation_path_sha256="f" * 64,
            phase="mutation_verified",
        )
        with pytest.raises(StateError, match="cannot be rebound"):
            record_live_apply_progress(store, conflicting)


def test_missing_or_changed_intent_binding_fails_closed(tmp_path: Path) -> None:
    prepared = _prepared()
    with StateStore(tmp_path) as store:
        operations_sha256, intent_record_sha256 = _prepare_store(store, prepared)
        progress = _progress(
            prepared,
            operations_sha256,
            intent_record_sha256,
            operation_index=0,
            path="automations.yaml",
        )
        store._connection.execute(
            "UPDATE live_apply_intent SET operations_sha256 = ? WHERE deployment_id = ?",
            ("9" * 64, prepared.deployment_id),
        )
        store._connection.commit()
        with pytest.raises(StateError, match="intent"):
            record_live_apply_progress(store, progress)


def test_tampered_progress_fails_closed_without_leaking_persisted_values(tmp_path: Path) -> None:
    prepared = _prepared()
    with StateStore(tmp_path) as store:
        operations_sha256, intent_record_sha256 = _prepare_store(store, prepared)
        progress = _progress(
            prepared,
            operations_sha256,
            intent_record_sha256,
            operation_index=0,
            path="automations.yaml",
        )
        record_live_apply_progress(store, progress)

    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE live_apply_progress SET operation_path_sha256 = ? "
            "WHERE deployment_id = ? AND operation_index = 0",
            ("PRIVATE-SENTINEL", prepared.deployment_id),
        )

    with StateStore(tmp_path) as store:
        with pytest.raises(StateError) as caught:
            load_live_apply_progress(store, prepared.deployment_id, 0)
        assert "PRIVATE-SENTINEL" not in str(caught.value)
        assert str(caught.value) == "Invalid live Apply progress record"
