from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from ha_syncapp.administrative_retry_request import (
    AdministrativeRetryRequest,
    AdministrativeRetryRequestError,
    apply_administrative_retry_request,
    load_administrative_retry_receipt,
)
from ha_syncapp.state import SCHEMA_VERSION, StateError, StateStore

NOW = datetime(2026, 9, 29, 1, 45, tzinfo=UTC)
REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"
KIND = "adminsentinel"
KEY = "secret-work-key-sentinel"


def _block(store: StateStore, *, key: str = KEY) -> None:
    store.enqueue_work(KIND, key, now=NOW - timedelta(minutes=2))
    claimed = store.claim_work_kind(KIND, now=NOW - timedelta(minutes=1))
    assert claimed is not None
    store.fail_work(claimed, transient=False, now=NOW - timedelta(seconds=30))


def _request(*, request_id: str = REQUEST_ID, key: str = KEY) -> AdministrativeRetryRequest:
    return AdministrativeRetryRequest(request_id=request_id, work_kind=KIND, work_key=key)


def test_unseen_request_atomically_retries_only_exact_blocked_item(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        _block(store)
        _block(store, key="other-key")

        result = apply_administrative_retry_request(store, _request(), now=NOW)
        retried = store._get_work(KIND, KEY)
        unchanged = store._get_work(KIND, "other-key")
        receipt = load_administrative_retry_receipt(store, REQUEST_ID)

    assert result.outcome == "retried"
    assert result.replayed is False
    assert retried.status == "pending"
    assert retried.attempts == 0
    assert retried.next_attempt_at == NOW
    assert unchanged.status == "blocked"
    assert receipt is not None
    assert receipt.request_id == REQUEST_ID
    assert receipt.outcome == "retried"
    assert receipt.processed_at == NOW
    assert len(receipt.identity_sha256) == 64
    assert KEY not in repr(receipt)


def test_same_request_replay_never_rearms_work_again(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        _block(store)
        first = apply_administrative_retry_request(store, _request(), now=NOW)
        running = store.claim_work_kind(KIND, now=NOW)
        assert running is not None
        store.fail_work(running, transient=False, now=NOW + timedelta(seconds=1))

        replay = apply_administrative_retry_request(
            store,
            _request(),
            now=NOW + timedelta(minutes=1),
        )
        still_blocked = store._get_work(KIND, KEY)

    assert first.replayed is False
    assert replay.outcome == "retried"
    assert replay.replayed is True
    assert still_blocked.status == "blocked"


def test_missing_or_nonblocked_target_is_durably_rejected(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        result = apply_administrative_retry_request(store, _request(), now=NOW)
        replay = apply_administrative_retry_request(
            store,
            _request(),
            now=NOW + timedelta(minutes=1),
        )
        receipt = load_administrative_retry_receipt(store, REQUEST_ID)

    assert result.outcome == "rejected"
    assert result.replayed is False
    assert replay.outcome == "rejected"
    assert replay.replayed is True
    assert receipt is not None
    assert receipt.outcome == "rejected"


def test_request_id_cannot_be_rebound_to_another_identity(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        apply_administrative_retry_request(store, _request(), now=NOW)

        with pytest.raises(AdministrativeRetryRequestError, match="failed closed") as error:
            apply_administrative_retry_request(
                store,
                _request(key="different-secret-key"),
                now=NOW + timedelta(minutes=1),
            )

    assert KEY not in str(error.value)
    assert "different-secret-key" not in str(error.value)


def test_receipt_failure_rolls_back_work_transition(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        _block(store)
        store._connection.execute(
            "CREATE TRIGGER reject_admin_receipt BEFORE INSERT ON "
            "administrative_retry_request BEGIN SELECT RAISE(ABORT, 'secret'); END"
        )

        with pytest.raises(AdministrativeRetryRequestError, match="failed closed") as error:
            apply_administrative_retry_request(store, _request(), now=NOW)
        blocked = store._get_work(KIND, KEY)

    assert blocked.status == "blocked"
    assert KEY not in str(error.value)


def test_receipt_tampering_and_noncanonical_request_fail_closed(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        apply_administrative_retry_request(store, _request(), now=NOW)
        store._connection.execute(
            "UPDATE administrative_retry_request SET outcome='retried' WHERE request_id=?",
            (REQUEST_ID,),
        )
        store._connection.execute(
            "UPDATE administrative_retry_request SET identity_sha256=? WHERE request_id=?",
            ("0" * 64, REQUEST_ID),
        )
        with pytest.raises(AdministrativeRetryRequestError, match="failed closed"):
            load_administrative_retry_receipt(store, REQUEST_ID)

        for request in (
            _request(request_id=str(UUID(REQUEST_ID)).upper()),
            AdministrativeRetryRequest(REQUEST_ID, "Bad Kind", KEY),
            AdministrativeRetryRequest(REQUEST_ID, KIND, "bad\x00key"),
        ):
            with pytest.raises(AdministrativeRetryRequestError, match="failed closed"):
                apply_administrative_retry_request(store, request, now=NOW)


def test_schema_34_migrates_receipts_without_losing_blocked_work(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        _block(store)
        store._connection.execute("DROP TABLE IF EXISTS administrative_retry_request")
        store._connection.execute("PRAGMA user_version = 34")

    with StateStore(tmp_path) as migrated:
        assert SCHEMA_VERSION == 36
        assert migrated._connection.execute("PRAGMA user_version").fetchone() == (36,)
        assert migrated._get_work(KIND, KEY).status == "blocked"
        assert load_administrative_retry_receipt(migrated, REQUEST_ID) is None


def test_sqlite_receipt_contains_no_raw_work_identity(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        apply_administrative_retry_request(store, _request(), now=NOW)
    raw = (tmp_path / "syncapp" / "state.sqlite3").read_bytes()

    assert KEY.encode() not in raw
    assert KIND.encode() not in raw


def test_runtime_projection_selects_only_outcome_and_timestamp(tmp_path: Path) -> None:
    statements: list[str] = []
    with StateStore(tmp_path) as store:
        apply_administrative_retry_request(store, _request(), now=NOW)
        store._connection.set_trace_callback(statements.append)
        evidence = store.administrative_retry_runtime_evidence()
        store._connection.set_trace_callback(None)

    assert [(row.outcome, row.processed_at) for row in evidence] == [("rejected", NOW)]
    statement = next(
        sql for sql in statements if "FROM administrative_retry_request" in sql
    ).lower()
    assert "select outcome, processed_at" in statement
    for forbidden in ("request_id", "identity_sha256", "record_sha256", "work_kind", "work_key"):
        assert forbidden not in statement
    assert REQUEST_ID not in repr(evidence)
    assert KEY not in repr(evidence)


def test_runtime_projection_is_bounded_and_fails_closed_on_invalid_time(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path) as store:
        rows = [
            (
                f"request-{index}",
                "0" * 64,
                "rejected",
                NOW.isoformat(),
                "1" * 64,
            )
            for index in range(4097)
        ]
        store._connection.executemany(
            "INSERT INTO administrative_retry_request VALUES (?,?,?,?,?)",
            rows,
        )
        with pytest.raises(StateError, match="exceeds the limit"):
            store.administrative_retry_runtime_evidence()

        store._connection.execute("DELETE FROM administrative_retry_request")
        store._connection.execute(
            "INSERT INTO administrative_retry_request VALUES (?,?,?,?,?)",
            ("request-invalid", "0" * 64, "rejected", "not-a-time", "1" * 64),
        )
        with pytest.raises(StateError, match="Invalid state timestamp"):
            store.administrative_retry_runtime_evidence()
