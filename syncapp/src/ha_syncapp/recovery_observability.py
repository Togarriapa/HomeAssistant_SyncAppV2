"""Read-only, sanitized observability for durable Retrigger work."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TypedDict

from .state import StateStore, WorkItem


class RecoveryOperation(TypedDict):
    operation_id: str
    work_kind: str
    status: str
    attempts: int
    created_at: str
    updated_at: str
    next_attempt_at: str | None
    retry_state: str | None


class RecoverySummary(TypedDict):
    total: int
    pending: int
    running: int
    retry: int
    blocked: int
    succeeded: int


class RecoveryStatus(TypedDict):
    generated_at: str
    summary: RecoverySummary
    operations: list[RecoveryOperation]


def build_recovery_status(
    store: StateStore,
    *,
    reference_time: datetime,
) -> RecoveryStatus:
    """Render durable work without exposing raw caller-controlled work keys."""
    if type(store) is not StateStore:
        raise TypeError("recovery state store is invalid")
    if not isinstance(reference_time, datetime) or reference_time.tzinfo is None:
        raise ValueError("recovery reference time must be timezone-aware")
    reference = reference_time.astimezone(UTC)
    items = store.recovery_work_items()
    summary: RecoverySummary = {
        "total": len(items),
        "pending": 0,
        "running": 0,
        "retry": 0,
        "blocked": 0,
        "succeeded": 0,
    }
    operations: list[RecoveryOperation] = []
    for item in items:
        summary[item.status] += 1  # type: ignore[literal-required]
        operations.append(_operation(item, reference))
    return {
        "generated_at": reference.isoformat(),
        "summary": summary,
        "operations": operations,
    }


def _operation(item: WorkItem, reference: datetime) -> RecoveryOperation:
    identity = hashlib.sha256(
        (item.work_kind + "\0" + item.work_key).encode("utf-8")
    ).hexdigest()[:32]
    retry_state: str | None = None
    if item.status == "retry":
        if item.next_attempt_at is None:
            raise ValueError("retry work is missing next-attempt evidence")
        retry_state = "due" if item.next_attempt_at <= reference else "deferred"
    return {
        "operation_id": identity,
        "work_kind": item.work_kind,
        "status": item.status,
        "attempts": item.attempts,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "next_attempt_at": (
            None if item.next_attempt_at is None else item.next_attempt_at.isoformat()
        ),
        "retry_state": retry_state,
    }
