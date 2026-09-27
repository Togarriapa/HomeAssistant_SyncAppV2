from __future__ import annotations

import sqlite3
from datetime import timedelta

from ha_syncapp.candidate_orchestration import static_validated_candidate_orchestration
from ha_syncapp.state import SCHEMA_VERSION, StateStore
from test_candidate_integrity_execution import NOW
from test_candidate_risk_execution import _ready_for_risk
from ha_syncapp.candidate_risk_execution import execute_candidate_risk_once


def test_schema_v31_contains_candidate_static_validation_checkpoint(tmp_path) -> None:
    assert SCHEMA_VERSION == 31
    with StateStore(tmp_path):
        pass
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 31
        columns = tuple(
            row[1] for row in db.execute("PRAGMA table_info(candidate_static_validation_checkpoint)")
        )
    assert columns == (
        "candidate_sha",
        "schema_version",
        "orchestration_sha256",
        "risk_sha256",
        "target",
        "repository_id",
        "baseline_sha",
        "stage_manifest_sha256",
        "phase",
        "validation_json",
        "syntax_valid",
        "invalid_count",
        "unvalidated_count",
        "planned_at",
        "completed_at",
        "record_sha256",
    )


def test_static_validation_successor_cannot_skip_semantic_validation(tmp_path) -> None:
    store, dependency = _ready_for_risk(tmp_path)
    try:
        risk = execute_candidate_risk_once(
            store, dependency.orchestration, now=NOW + timedelta(seconds=2)
        )
        successor = static_validated_candidate_orchestration(
            risk.orchestration, updated_at=NOW + timedelta(seconds=3)
        )
        assert successor.phase == "static_validated"
        assert successor.next_action == "validate_home_assistant"
    finally:
        store.__exit__(None, None, None)
