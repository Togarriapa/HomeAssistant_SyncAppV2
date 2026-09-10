"""Restart-safe immutable candidate/backup associations (issue #208)."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.state import StateError, StateStore

WHEN = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)
DEPLOYMENT = "11111111-1111-4111-8111-111111111111"
EVIDENCE = CandidateBackupEvidence(
    target="Owner/Home",
    repository_id=123,
    baseline_sha="a" * 40,
    candidate_sha="b" * 40,
    stage_manifest_sha256="c" * 64,
    runtime_sha256="d" * 64,
    risk_level="high",
    core_version="2026.9.1",
    backup_slug="backup-123",
)


def _record(store, evidence=EVIDENCE, deployment_id=DEPLOYMENT, when=WHEN):
    return store.record_prepared_deployment(deployment_id, evidence, prepared_at=when)


def test_prepared_record_survives_restart_and_replay_preserves_timestamp(tmp_path):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        assert store.prepared_deployment(DEPLOYMENT) is None
        record = _record(store)
        assert record.deployment_id == DEPLOYMENT
        assert record.evidence == EVIDENCE
        assert record.prepared_at == WHEN
        assert _record(store, when=WHEN + timedelta(hours=1)) == record
    with StateStore(tmp_path) as store:
        assert store.prepared_deployment(DEPLOYMENT) == record
        assert _record(store) == record


@pytest.mark.parametrize("bound_id", [None, 124])
def test_unpinned_or_changed_repository_is_rejected(tmp_path, bound_id):
    with StateStore(tmp_path) as store:
        if bound_id is not None:
            store.bind_repository(EVIDENCE.target, bound_id)
        with pytest.raises(StateError):
            _record(store)
        assert store.prepared_deployment(DEPLOYMENT) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"backup_slug": "different"},
        {"baseline_sha": "e" * 40},
        {"candidate_sha": "e" * 40},
        {"stage_manifest_sha256": "e" * 64},
        {"runtime_sha256": "e" * 64},
        {"risk_level": "low"},
        {"core_version": "2026.9.2"},
        {"target": "Owner/Other"},
        {"repository_id": 124},
    ],
)
def test_deployment_id_cannot_be_rebound(tmp_path, changes):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        store.bind_repository("Owner/Other", EVIDENCE.repository_id)
        original = _record(store)
        with pytest.raises(StateError):
            _record(store, replace(EVIDENCE, **changes))
        assert store.prepared_deployment(DEPLOYMENT) == original


@pytest.mark.parametrize("changed_backup", [False, True])
def test_candidate_cannot_receive_another_deployment_identity(tmp_path, changed_backup):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        original = _record(store)
        other_id = str(uuid4())
        evidence = replace(EVIDENCE, backup_slug="other") if changed_backup else EVIDENCE
        with pytest.raises(StateError):
            _record(store, evidence, other_id)
        assert store.prepared_deployment(other_id) is None
        assert store.prepared_deployment(DEPLOYMENT) == original
        # A genuinely new candidate has a new preparation identity.
        assert _record(store, replace(EVIDENCE, candidate_sha="e" * 40), other_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"target": "secret-sentinel\n"},
        {"repository_id": True},
        {"repository_id": 0},
        {"baseline_sha": "bad"},
        {"candidate_sha": "bad"},
        {"candidate_sha": "a" * 40},
        {"candidate_sha": "b" * 64},
        {"stage_manifest_sha256": None},
        {"runtime_sha256": "g" * 64},
        {"risk_level": "secret-sentinel"},
        {"core_version": "latest"},
        {"backup_slug": "../secret-sentinel"},
    ],
)
def test_malformed_evidence_fails_closed_without_disclosure(tmp_path, changes):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        with pytest.raises(StateError) as caught:
            _record(store, replace(EVIDENCE, **changes))
        assert "secret-sentinel" not in str(caught.value)
        assert store.prepared_deployment(DEPLOYMENT) is None


@pytest.mark.parametrize("evidence", [None, {}, "secret-sentinel"])
def test_only_typed_backup_evidence_is_accepted(tmp_path, evidence):
    with StateStore(tmp_path) as store, pytest.raises(StateError):
        _record(store, evidence)


@pytest.mark.parametrize("deployment_id", ["", "secret-sentinel", None])
def test_invalid_deployment_id_is_rejected(tmp_path, deployment_id):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        with pytest.raises(StateError):
            _record(store, deployment_id=deployment_id)
        with pytest.raises(StateError):
            store.prepared_deployment(deployment_id)


@pytest.mark.parametrize("when", [datetime(2026, 9, 10), "secret-sentinel"])
def test_invalid_preparation_time_is_rejected(tmp_path, when):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        with pytest.raises(StateError):
            _record(store, when=when)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("backup_slug", "other-valid-slug"),
        ("candidate_sha", "f" * 40),
        ("runtime_sha256", "f" * 64),
        ("risk_level", "invalid-secret-sentinel"),
        ("core_version", "latest"),
        ("prepared_at", "2026-09-10T19:00:00"),
        ("record_sha256", "f" * 64),
    ],
)
def test_corrupt_persisted_evidence_cannot_be_read_or_repaired(tmp_path, column, value):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        _record(store)
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE prepared_deployment SET {column} = ?", (value,))
    before = path.read_bytes()
    with StateStore(tmp_path) as store:
        with pytest.raises(StateError) as caught:
            store.prepared_deployment(DEPLOYMENT)
        assert "secret-sentinel" not in str(caught.value)
        with pytest.raises(StateError):
            _record(store)
    assert path.read_bytes() == before


def test_read_rechecks_current_repository_binding(tmp_path):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        _record(store)
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as db:
        db.execute("UPDATE repository_binding SET repository_id = 124")
    with StateStore(tmp_path) as store, pytest.raises(StateError):
        store.prepared_deployment(DEPLOYMENT)


def test_v4_migration_preserves_all_existing_state(tmp_path):
    with StateStore(tmp_path) as store:
        boot = store.start_run()
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        work = store.enqueue_work("candidate", EVIDENCE.candidate_sha, now=WHEN)
        baseline = store.record_synchronization_baseline(
            EVIDENCE.target, "main", "a" * 64, EVIDENCE.baseline_sha, synchronized_at=WHEN
        )
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE prepared_deployment")
        db.execute("PRAGMA user_version = 4")
    with StateStore(tmp_path) as store:
        next_boot = store.start_run()
        assert next_boot.installation_id == boot.installation_id
        assert next_boot.interrupted_run_id == boot.run_id
        assert next_boot.boot_count == boot.boot_count + 1
        assert store.repository_id(EVIDENCE.target) == EVIDENCE.repository_id
        assert store.synchronization_baseline(EVIDENCE.target, "main") == baseline
        assert store.enqueue_work("candidate", EVIDENCE.candidate_sha, now=WHEN) == work
        assert _record(store).evidence == EVIDENCE
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5


def test_insert_failure_is_atomic_and_sanitized(tmp_path):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as db:
        db.execute(
            "CREATE TRIGGER fail_prepare BEFORE INSERT ON prepared_deployment "
            "BEGIN SELECT RAISE(ABORT, 'secret-sentinel'); END"
        )
    with StateStore(tmp_path) as store:
        with pytest.raises(StateError) as caught:
            _record(store)
        assert "secret-sentinel" not in str(caught.value)
        assert store.prepared_deployment(DEPLOYMENT) is None


def test_v4_migration_failure_does_not_advance_schema_or_replace_state(tmp_path):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        _record(store)
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        # An unexpected pre-existing table must never be adopted or replaced.
        db.execute("PRAGMA user_version = 4")
    before = path.read_bytes()
    with pytest.raises(StateError), StateStore(tmp_path):
        pytest.fail("unexpected preparation table was adopted")
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            db.execute("SELECT backup_slug FROM prepared_deployment").fetchone()[0] == "backup-123"
        )


def test_repository_alias_cannot_rebind_same_candidate(tmp_path):
    with StateStore(tmp_path) as store:
        store.bind_repository(EVIDENCE.target, EVIDENCE.repository_id)
        store.bind_repository("Owner/Renamed", EVIDENCE.repository_id)
        original = _record(store)
        with pytest.raises(StateError):
            _record(store, replace(EVIDENCE, target="Owner/Renamed"), str(uuid4()))
        assert store.prepared_deployment(DEPLOYMENT) == original
