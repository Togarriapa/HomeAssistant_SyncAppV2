from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.candidate_orchestration import (
    CandidateOrchestrationError,
    candidate_orchestration_runtime_evidence,
    discover_candidate_orchestrations,
    load_candidate_orchestration,
    register_claimed_candidate,
)
from ha_syncapp.state import StateStore

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
TARGET = "owner/home-assistant-config"
REPOSITORY_ID = 42
CANDIDATE_SHA = "a" * 40


def _store(tmp_path: Path) -> StateStore:
    root = tmp_path / "data"
    root.mkdir()
    store = StateStore(root)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store


def _claimed_candidate(store: StateStore):
    store.enqueue_work("candidate", CANDIDATE_SHA, now=NOW)
    claimed = store.claim_work_kind("candidate", now=NOW)
    assert claimed is not None
    return claimed


def test_claimed_candidate_is_registered_with_only_first_safe_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        record = register_claimed_candidate(
            store,
            _claimed_candidate(store),
            target=TARGET,
            repository_id=REPOSITORY_ID,
            now=NOW,
        )

        assert record.schema_version == 1
        assert record.target == TARGET
        assert record.repository_id == REPOSITORY_ID
        assert record.candidate_sha == CANDIDATE_SHA
        assert record.phase == "detected"
        assert record.next_action == "fetch_stage"
        assert record.registered_at == NOW
        assert record.updated_at == NOW
        assert len(record.record_sha256) == 64
        assert load_candidate_orchestration(store, CANDIDATE_SHA) == record
    finally:
        store.__exit__(None, None, None)


def test_registration_is_idempotent_for_exact_claim_and_binding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        claimed = _claimed_candidate(store)
        first = register_claimed_candidate(
            store,
            claimed,
            target=TARGET,
            repository_id=REPOSITORY_ID,
            now=NOW,
        )
        replay = register_claimed_candidate(
            store,
            claimed,
            target=TARGET,
            repository_id=REPOSITORY_ID,
            now=NOW.replace(minute=1),
        )
        assert replay == first
    finally:
        store.__exit__(None, None, None)


def test_unclaimed_or_rebound_candidate_cannot_be_registered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        pending = store.enqueue_work("candidate", CANDIDATE_SHA, now=NOW)
        with pytest.raises(CandidateOrchestrationError, match="unavailable"):
            register_claimed_candidate(
                store,
                pending,
                target=TARGET,
                repository_id=REPOSITORY_ID,
                now=NOW,
            )

        claimed = store.claim_work_kind("candidate", now=NOW)
        assert claimed is not None
        with pytest.raises(CandidateOrchestrationError, match="unavailable"):
            register_claimed_candidate(
                store,
                claimed,
                target=TARGET,
                repository_id=REPOSITORY_ID + 1,
                now=NOW,
            )
    finally:
        store.__exit__(None, None, None)


def test_transient_retry_can_register_but_deterministic_block_cannot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        first = _claimed_candidate(store)
        retry = store.fail_work(first, transient=True, now=NOW)
        assert retry.status == "retry"
        retried = store.claim_work_kind("candidate", now=NOW + timedelta(seconds=60))
        assert retried is not None
        record = register_claimed_candidate(
            store,
            retried,
            target=TARGET,
            repository_id=REPOSITORY_ID,
            now=NOW + timedelta(seconds=60),
        )
        assert record.next_action == "fetch_stage"

        other_sha = "b" * 40
        store.enqueue_work("candidate", other_sha, now=NOW)
        deterministic = store.claim_work_kind("candidate", now=NOW)
        assert deterministic is not None and deterministic.work_key == other_sha
        blocked = store.fail_work(deterministic, transient=False, now=NOW)
        assert blocked.status == "blocked"
        with pytest.raises(CandidateOrchestrationError, match="unavailable"):
            register_claimed_candidate(
                store,
                deterministic,
                target=TARGET,
                repository_id=REPOSITORY_ID,
                now=NOW,
            )
        assert load_candidate_orchestration(store, other_sha) is None
    finally:
        store.__exit__(None, None, None)


def test_integrity_tampering_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        register_claimed_candidate(
            store,
            _claimed_candidate(store),
            target=TARGET,
            repository_id=REPOSITORY_ID,
            now=NOW,
        )
        store._connection.execute(
            "UPDATE candidate_orchestration SET next_action = 'none' WHERE candidate_sha = ?",
            (CANDIDATE_SHA,),
        )
        with pytest.raises(CandidateOrchestrationError, match="invalid"):
            load_candidate_orchestration(store, CANDIDATE_SHA)
    finally:
        store.__exit__(None, None, None)


def test_discovery_is_bounded_canonical_and_network_free(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        record = register_claimed_candidate(
            store,
            _claimed_candidate(store),
            target=TARGET,
            repository_id=REPOSITORY_ID,
            now=NOW,
        )
        assert discover_candidate_orchestrations(store) == (record,)
        runtime = candidate_orchestration_runtime_evidence(store)
        assert len(runtime) == 1
        assert runtime[0].phase == "detected"
        assert runtime[0].next_action == "fetch_stage"
        assert runtime[0].updated_at == NOW
        assert CANDIDATE_SHA not in repr(runtime)
        assert TARGET not in repr(runtime)
    finally:
        store.__exit__(None, None, None)
