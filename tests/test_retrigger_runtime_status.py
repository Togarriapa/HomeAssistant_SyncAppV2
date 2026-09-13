from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.retrigger_runtime_status import (
    MAX_RECOVERY_EVIDENCE_ROWS,
    RetriggerRuntimeStatusError,
    collect_retrigger_runtime_inventory,
    render_retrigger_runtime_status,
)
from ha_syncapp.state import RecoveryWorkEvidence, StateStore

NOW = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
STATUSES = {"pending", "running", "retry", "blocked", "succeeded"}


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_collects_aggregate_status_attempt_and_backoff_without_work_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.enqueue_work("candidate", "candidate-secret-sha", now=NOW)
        running = store.enqueue_work("local_sync", "/secret/source/path", now=NOW)
        running = store.claim_work_kind(running.work_kind, now=NOW)
        assert running is not None

        retry = store.enqueue_work("database", "repo:/secret/db", now=NOW)
        retry = store.claim_work_kind(retry.work_kind, now=NOW)
        assert retry is not None
        store.fail_work(retry, transient=True, now=NOW)

        blocked = store.enqueue_work("logs", "token-like-secret", now=NOW)
        blocked = store.claim_work_kind(blocked.work_kind, now=NOW)
        assert blocked is not None
        store.fail_work(blocked, transient=False, now=NOW)

        succeeded = store.enqueue_work("runtime", "runtime-secret", now=NOW)
        succeeded = store.claim_work_kind(succeeded.work_kind, now=NOW)
        assert succeeded is not None
        store.complete_work(succeeded, now=NOW)

        inventory = collect_retrigger_runtime_inventory(store, reference_time=NOW)
        second = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    recovery = inventory.analysis["recovery"]
    assert recovery == second.analysis["recovery"]
    assert recovery["total"] == 5
    assert set(recovery["statuses"]) == STATUSES
    assert recovery["statuses"] == {
        "blocked": 1,
        "pending": 1,
        "retry": 1,
        "running": 1,
        "succeeded": 1,
    }
    kinds = {item["kind"]: item for item in recovery["kinds"]}
    assert kinds["database"]["backoff"] == {
        "scheduled": 1,
        "next_attempt_at": "2026-09-13T01:01:00+00:00",
    }
    assert kinds["database"]["attempts"] == {"maximum": 1, "total": 1}
    encoded = json.dumps(recovery, sort_keys=True)
    for secret in (
        "candidate-secret-sha",
        "/secret/source/path",
        "repo:/secret/db",
        "token-like-secret",
        "runtime-secret",
    ):
        assert secret not in encoded


def test_empty_status_is_explicit_and_deterministic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        first = collect_retrigger_runtime_inventory(store, reference_time=NOW)
        second = collect_retrigger_runtime_inventory(store, reference_time=NOW)
    finally:
        store.__exit__(None, None, None)

    assert first == second
    assert first.analysis["recovery"] == {
        "reference_time": "2026-09-13T01:00:00+00:00",
        "total": 0,
        "statuses": {status: 0 for status in sorted(STATUSES)},
        "attempts": {"maximum": 0, "total": 0},
        "ready": 0,
        "backoff": {"scheduled": 0, "next_attempt_at": None},
        "kinds": [],
    }


def test_collection_is_read_only_for_pending_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        original = store.enqueue_work("candidate", "unchanged", now=NOW)
        collect_retrigger_runtime_inventory(store, reference_time=NOW)
        observed = store.enqueue_work("candidate", "unchanged", now=NOW + timedelta(hours=1))
    finally:
        store.__exit__(None, None, None)

    assert observed == original


def _evidence(**changes: object) -> RecoveryWorkEvidence:
    base = RecoveryWorkEvidence(
        work_kind="candidate",
        status="retry",
        attempts=1,
        created_at=NOW - timedelta(minutes=2),
        updated_at=NOW - timedelta(minutes=1),
        next_attempt_at=NOW + timedelta(minutes=1),
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(work_kind="../secret"),
        _evidence(status="unknown"),
        _evidence(attempts=-1),
        _evidence(attempts=StateStore.MAX_WORK_ATTEMPTS + 1),
        _evidence(created_at=NOW.replace(tzinfo=None)),
        _evidence(updated_at=NOW - timedelta(hours=1)),
        _evidence(status="blocked", next_attempt_at=NOW),
        _evidence(status="retry", next_attempt_at=None),
    ],
)
def test_render_rejects_malformed_evidence(evidence: RecoveryWorkEvidence) -> None:
    with pytest.raises(RetriggerRuntimeStatusError, match="evidence is invalid"):
        render_retrigger_runtime_status((evidence,), reference_time=NOW)


def test_render_rejects_invalid_reference_time() -> None:
    with pytest.raises(RetriggerRuntimeStatusError, match="reference time is invalid"):
        render_retrigger_runtime_status((), reference_time=NOW.replace(tzinfo=None))


def test_render_bounds_evidence_rows() -> None:
    evidence = tuple(_evidence() for _ in range(MAX_RECOVERY_EVIDENCE_ROWS + 1))
    with pytest.raises(RetriggerRuntimeStatusError, match="limit"):
        render_retrigger_runtime_status(evidence, reference_time=NOW)
