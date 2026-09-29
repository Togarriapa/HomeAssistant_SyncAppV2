"""Crash-safe, one-shot authorization for an exact blocked work retry."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn
from uuid import UUID

from .state import (
    StateError,
    StateStore,
    _parse_timestamp,
    _timestamp,
    _validate_work_identity,
)

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class AdministrativeRetryRequestError(RuntimeError):
    """A one-shot retry request or its durable receipt failed closed."""


@dataclass(frozen=True, slots=True)
class AdministrativeRetryRequest:
    """Exact operator request; its durable receipt never retains the raw identity."""

    request_id: str
    work_kind: str = field(repr=False)
    work_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AdministrativeRetryReceipt:
    """Content-free durable outcome for one unique request."""

    request_id: str
    identity_sha256: str
    outcome: str
    processed_at: datetime
    record_sha256: str


@dataclass(frozen=True, slots=True)
class AdministrativeRetryResult:
    """Sanitized result used by startup logging."""

    outcome: str
    replayed: bool


def apply_administrative_retry_request(
    store: StateStore,
    request: AdministrativeRetryRequest,
    *,
    now: datetime | None = None,
) -> AdministrativeRetryResult:
    """Consume one request and retry at most its exact blocked item atomically."""
    if type(store) is not StateStore or type(request) is not AdministrativeRetryRequest:
        raise AdministrativeRetryRequestError("administrative retry request failed closed")
    try:
        _validate_request(request)
        processed_at = _timestamp(now)
        identity_sha256 = _identity_digest(request.work_kind, request.work_key)
        with store._connection as database:
            database.execute("BEGIN IMMEDIATE")
            existing = _load_receipt(database, request.request_id)
            if existing is not None:
                if existing.identity_sha256 != identity_sha256:
                    _invalid()
                return AdministrativeRetryResult(existing.outcome, True)

            row = database.execute(
                "SELECT work_kind, work_key, status, attempts, created_at, updated_at, "
                "next_attempt_at FROM work WHERE work_kind=? AND work_key=?",
                (request.work_kind, request.work_key),
            ).fetchone()
            outcome = "rejected"
            blocked = None if row is None else store._work_from_row(row)
            if blocked is not None and blocked.status == "blocked":
                current = processed_at.isoformat()
                updated = database.execute(
                    "UPDATE work SET status='pending', attempts=0, updated_at=?, "
                    "next_attempt_at=? WHERE work_kind=? AND work_key=? "
                    "AND status='blocked' AND attempts=?",
                    (
                        current,
                        current,
                        request.work_kind,
                        request.work_key,
                        blocked.attempts,
                    ),
                )
                if updated.rowcount != 1:
                    _invalid()
                outcome = "retried"

            receipt = _new_receipt(
                request.request_id,
                identity_sha256,
                outcome,
                processed_at,
            )
            database.execute(
                "INSERT INTO administrative_retry_request "
                "(request_id,identity_sha256,outcome,processed_at,record_sha256) "
                "VALUES (?,?,?,?,?)",
                (
                    receipt.request_id,
                    receipt.identity_sha256,
                    receipt.outcome,
                    receipt.processed_at.isoformat(),
                    receipt.record_sha256,
                ),
            )
            persisted = _load_receipt(database, request.request_id)
            if persisted != receipt:
                _invalid()
            if outcome == "retried":
                if blocked is None:
                    _invalid()
                retried = store._get_work(request.work_kind, request.work_key)
                if (
                    retried.status != "pending"
                    or retried.attempts != 0
                    or retried.created_at != blocked.created_at
                    or retried.updated_at != processed_at
                    or retried.next_attempt_at != processed_at
                ):
                    _invalid()
        return AdministrativeRetryResult(outcome, False)
    except AdministrativeRetryRequestError:
        raise
    except (StateError, sqlite3.Error, TypeError, ValueError):
        _invalid()


def load_administrative_retry_receipt(
    store: StateStore,
    request_id: str,
) -> AdministrativeRetryReceipt | None:
    """Load and integrity-check one content-free receipt."""
    if type(store) is not StateStore:
        raise AdministrativeRetryRequestError("administrative retry request failed closed")
    try:
        _validate_request_id(request_id)
        return _load_receipt(store._connection, request_id)
    except AdministrativeRetryRequestError:
        raise
    except (StateError, sqlite3.Error, TypeError, ValueError):
        _invalid()


def _load_receipt(
    database: sqlite3.Connection,
    request_id: str,
) -> AdministrativeRetryReceipt | None:
    rows = database.execute(
        "SELECT request_id,identity_sha256,outcome,processed_at,record_sha256 "
        "FROM administrative_retry_request WHERE request_id=?",
        (request_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 5:
        _invalid()
    stored_id, identity_sha256, outcome, processed_at, record_sha256 = rows[0]
    _validate_request_id(stored_id)
    if (
        type(identity_sha256) is not str
        or _HEX_64.fullmatch(identity_sha256) is None
        or outcome not in {"retried", "rejected"}
        or type(record_sha256) is not str
        or _HEX_64.fullmatch(record_sha256) is None
    ):
        _invalid()
    when = _parse_timestamp(processed_at)
    expected = _receipt_digest(stored_id, identity_sha256, outcome, when)
    if record_sha256 != expected:
        _invalid()
    return AdministrativeRetryReceipt(stored_id, identity_sha256, outcome, when, record_sha256)


def _new_receipt(
    request_id: str,
    identity_sha256: str,
    outcome: str,
    processed_at: datetime,
) -> AdministrativeRetryReceipt:
    return AdministrativeRetryReceipt(
        request_id,
        identity_sha256,
        outcome,
        processed_at,
        _receipt_digest(request_id, identity_sha256, outcome, processed_at),
    )


def _identity_digest(work_kind: str, work_key: str) -> str:
    encoded = json.dumps(
        [work_kind, work_key],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _receipt_digest(
    request_id: str,
    identity_sha256: str,
    outcome: str,
    processed_at: datetime,
) -> str:
    encoded = json.dumps(
        [1, request_id, identity_sha256, outcome, processed_at.isoformat()],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _validate_request(request: AdministrativeRetryRequest) -> None:
    _validate_request_id(request.request_id)
    _validate_work_identity(request.work_kind, request.work_key)


def _validate_request_id(value: object) -> None:
    if type(value) is not str:
        _invalid()
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        _invalid()


def _invalid() -> NoReturn:
    raise AdministrativeRetryRequestError("administrative retry request failed closed") from None
