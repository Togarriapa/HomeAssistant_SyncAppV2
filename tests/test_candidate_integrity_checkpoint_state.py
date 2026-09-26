from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ha_syncapp.candidate_changes import CandidateChange, CandidateChanges
from ha_syncapp.candidate_integrity_checkpoint import CandidateIntegrityCheckpoint
from ha_syncapp.state import SCHEMA_VERSION, StateStore

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
TARGET = "owner/home-assistant-config"
SHA = "a" * 40
BASELINE = "b" * 40


def test_schema_28_adds_integrity_checkpoint_and_successor_authority(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with StateStore(data) as store:
        version = store._connection.execute("PRAGMA user_version").fetchone()
        table = store._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'candidate_integrity_checkpoint'"
        ).fetchone()
        orchestration = store._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'candidate_orchestration'"
        ).fetchone()

    assert SCHEMA_VERSION == 28
    assert version == (28,)
    assert table is not None
    assert "candidate_fetch_stage_checkpoint" in table[0]
    assert orchestration is not None
    assert "'integrity_verified'" in orchestration[0]
    assert "'analyze_dependencies'" in orchestration[0]


def test_schema_27_migration_preserves_candidate_work(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with StateStore(data) as store:
        store.bind_repository(TARGET, 42)
        store.enqueue_work("candidate", SHA, now=NOW)
        store._connection.execute("PRAGMA user_version = 27")
        store._connection.commit()

    database = data / "syncapp" / "state.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE candidate_integrity_checkpoint")
    connection.commit()
    connection.close()

    with StateStore(data) as migrated:
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (28,)
        work = migrated._get_work("candidate", SHA)

    assert work.work_key == SHA
    assert work.status == "pending"


def test_checkpoint_canonicalizes_exact_change_evidence() -> None:
    changes = CandidateChanges(
        target=TARGET,
        repository_id=42,
        baseline_sha=BASELINE,
        candidate_sha=SHA,
        changes=(
            CandidateChange(
                path="automations.yaml",
                status="modified",
                baseline_mode="100644",
                baseline_object_id="c" * 40,
                candidate_mode="100644",
                candidate_object_id="d" * 40,
            ),
        ),
    )
    planned = CandidateIntegrityCheckpoint.plan(
        candidate_sha=SHA,
        orchestration_sha256="e" * 64,
        fetch_stage_sha256="f" * 64,
        target=TARGET,
        repository_id=42,
        stage_manifest_sha256="1" * 64,
        planned_at=NOW,
    )

    completed = planned.complete(changes, completed_at=NOW)
    replayed_changes = completed.changes()

    assert completed.phase == "completed"
    assert completed.baseline_sha == BASELINE
    assert completed.changed_count == 1
    assert replayed_changes == changes
    assert " " not in completed.changes_json
