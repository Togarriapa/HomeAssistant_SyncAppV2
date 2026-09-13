from __future__ import annotations

from pathlib import Path

import pytest
from ha_syncapp import database_retention_work
from ha_syncapp.database_history_replace_transport import (
    DatabaseHistoryReplacementFailureKind,
    DatabaseHistoryReplacementTransportError,
)
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
HEAD = "1" * 40
RETENTION_DAYS = 7


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_retention_work_identity_is_exact_head_and_policy_bound() -> None:
    baseline = database_retention_work.database_retention_work_key(
        TARGET, REPOSITORY_ID, HEAD, RETENTION_DAYS
    )

    assert baseline == database_retention_work.database_retention_work_key(
        TARGET.lower(), REPOSITORY_ID, HEAD, RETENTION_DAYS
    )
    assert baseline != database_retention_work.database_retention_work_key(
        TARGET, REPOSITORY_ID + 1, HEAD, RETENTION_DAYS
    )
    assert baseline != database_retention_work.database_retention_work_key(
        TARGET, REPOSITORY_ID, "2" * 40, RETENTION_DAYS
    )
    assert baseline != database_retention_work.database_retention_work_key(
        TARGET, REPOSITORY_ID, HEAD, RETENTION_DAYS + 1
    )
    assert TARGET not in baseline
    assert HEAD not in baseline


def test_retention_enqueue_is_idempotent_for_same_verified_head(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        first = database_retention_work.enqueue_database_retention_work(
            store, TARGET, REPOSITORY_ID, HEAD, RETENTION_DAYS
        )
        second = database_retention_work.enqueue_database_retention_work(
            store, TARGET, REPOSITORY_ID, HEAD, RETENTION_DAYS
        )
    finally:
        store.__exit__(None, None, None)

    assert first.work_kind == "database-retention"
    assert second.work_key == first.work_key
    assert second.status == "pending"


def test_retention_claim_does_not_consume_other_work_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue_work("database", "database-a")
    database_retention_work.enqueue_database_retention_work(
        store, TARGET, REPOSITORY_ID, HEAD, RETENTION_DAYS
    )
    try:
        claimed = database_retention_work.claim_database_retention_work(store)
        unrelated = store.claim_work()
    finally:
        store.__exit__(None, None, None)

    assert claimed is not None and claimed.work_kind == "database-retention"
    assert unrelated is not None and unrelated.work_kind == "database"


@pytest.mark.parametrize(
    "kind",
    [
        DatabaseHistoryReplacementFailureKind.INVALID,
        DatabaseHistoryReplacementFailureKind.STALE,
        DatabaseHistoryReplacementFailureKind.REJECTED,
    ],
)
def test_deterministic_retention_failures_are_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: DatabaseHistoryReplacementFailureKind,
) -> None:
    store = _store(tmp_path)
    database_retention_work.enqueue_database_retention_work(
        store, TARGET, REPOSITORY_ID, HEAD, RETENTION_DAYS
    )
    item = database_retention_work.claim_database_retention_work(store)
    assert item is not None

    def fail(*args: object, **kwargs: object) -> bool:
        raise DatabaseHistoryReplacementTransportError("sanitized failure", kind=kind)

    monkeypatch.setattr(database_retention_work, "run_database_retention_cycle", fail)
    try:
        result = database_retention_work.execute_claimed_database_retention_work(
            store,
            item,
            TARGET,
            REPOSITORY_ID,
            HEAD,
            RETENTION_DAYS,
            "secret-token",
            tmp_path / "retention-staging",
        )
        assert database_retention_work.claim_database_retention_work(store) is None
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "blocked"
    assert result.work.next_attempt_at is None
    assert result.replaced is None
    assert "secret-token" not in repr(result)


def test_transient_retention_failure_enters_controlled_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    database_retention_work.enqueue_database_retention_work(
        store, TARGET, REPOSITORY_ID, HEAD, RETENTION_DAYS
    )
    item = database_retention_work.claim_database_retention_work(store)
    assert item is not None

    def fail(*args: object, **kwargs: object) -> bool:
        raise DatabaseHistoryReplacementTransportError(
            "sanitized failure",
            kind=DatabaseHistoryReplacementFailureKind.TRANSIENT,
        )

    monkeypatch.setattr(database_retention_work, "run_database_retention_cycle", fail)
    try:
        result = database_retention_work.execute_claimed_database_retention_work(
            store,
            item,
            TARGET,
            REPOSITORY_ID,
            HEAD,
            RETENTION_DAYS,
            "secret-token",
            tmp_path / "retention-staging",
        )
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "retry"
    assert result.work.next_attempt_at is not None
    assert result.replaced is None
    assert "secret-token" not in repr(result)


@pytest.mark.parametrize("replaced", [False, True])
def test_successful_retention_cycle_completes_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replaced: bool,
) -> None:
    store = _store(tmp_path)
    database_retention_work.enqueue_database_retention_work(
        store, TARGET, REPOSITORY_ID, HEAD, RETENTION_DAYS
    )
    item = database_retention_work.claim_database_retention_work(store)
    assert item is not None

    monkeypatch.setattr(
        database_retention_work,
        "run_database_retention_cycle",
        lambda *args, **kwargs: replaced,
    )
    try:
        result = database_retention_work.execute_claimed_database_retention_work(
            store,
            item,
            TARGET,
            REPOSITORY_ID,
            HEAD,
            RETENTION_DAYS,
            "secret-token",
            tmp_path / "retention-staging",
        )
    finally:
        store.__exit__(None, None, None)

    assert result.work.status == "succeeded"
    assert result.replaced is replaced


@pytest.mark.parametrize(
    ("target", "repository_id", "head", "retention_days"),
    [
        ("", REPOSITORY_ID, HEAD, RETENTION_DAYS),
        (TARGET, 0, HEAD, RETENTION_DAYS),
        (TARGET, REPOSITORY_ID, "bad", RETENTION_DAYS),
        (TARGET, REPOSITORY_ID, HEAD, 0),
        (TARGET, REPOSITORY_ID, HEAD, 366),
        (TARGET, True, HEAD, RETENTION_DAYS),
        (TARGET, REPOSITORY_ID, HEAD, True),
    ],
)
def test_retention_work_identity_rejects_invalid_boundary_values(
    target: str,
    repository_id: int,
    head: str,
    retention_days: int,
) -> None:
    with pytest.raises(database_retention_work.DatabaseRetentionWorkError):
        database_retention_work.database_retention_work_key(
            target, repository_id, head, retention_days
        )
