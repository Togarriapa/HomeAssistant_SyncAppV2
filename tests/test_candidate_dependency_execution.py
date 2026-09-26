from __future__ import annotations

from pathlib import Path

import pytest

from ha_syncapp.candidate_dependency_execution import (
    CandidateDependencyExecutionError,
    candidate_dependency_runtime_evidence,
)
from ha_syncapp.state import SCHEMA_VERSION, StateStore


def test_dependency_checkpoint_requires_schema_v29() -> None:
    assert SCHEMA_VERSION == 29


def test_fresh_store_contains_candidate_dependency_checkpoint(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        columns = tuple(
            row[1]
            for row in store._connection.execute(
                "PRAGMA table_info(candidate_dependency_checkpoint)"
            ).fetchall()
        )
    assert columns == (
        "candidate_sha",
        "schema_version",
        "orchestration_sha256",
        "integrity_checkpoint_sha256",
        "runtime_sha256",
        "phase",
        "dependency_sha256",
        "impact_sha256",
        "known_reference_count",
        "unresolved_reference_count",
        "affected_entity_count",
        "planned_at",
        "completed_at",
        "record_sha256",
    )


def test_empty_dependency_runtime_evidence_is_bounded(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        assert candidate_dependency_runtime_evidence(store) == ()


def test_dependency_execution_error_is_sanitized() -> None:
    error = CandidateDependencyExecutionError("Candidate dependency analysis is unavailable")
    assert "token" not in str(error).lower()
