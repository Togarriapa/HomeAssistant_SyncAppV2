"""Durable content-free guards and outcomes for live Apply reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from .live_apply_plan import LiveApplyPlan
from .live_apply_progress import LiveApplyProgress, validate_live_apply_progress_plan_binding
from .live_apply_progress_store import load_live_apply_progress
from .state import StateError, StateStore

_HASH = re.compile(r"^[0-9a-f]{64}$")
_OUTCOMES = {"applied", "not_applied", "ambiguous"}


@dataclass(frozen=True, slots=True)
class LiveApplyMutationGuard:
    deployment_id: str
    operation_index: int
    intent_record_sha256: str
    operations_sha256: str
    operation_path_sha256: str
    root_identity: tuple[int, int]
    parent_identities: tuple[tuple[int, int], ...]
    recorded_at: datetime
    record_sha256: str


@dataclass(frozen=True, slots=True)
class LiveApplyReconciliationRecord:
    deployment_id: str
    operation_index: int
    outcome: str
    recorded_at: datetime
    record_sha256: str


def record_live_apply_mutation_guard(
    store: StateStore,
    progress: LiveApplyProgress,
    *,
    plan: LiveApplyPlan,
    root_identity: tuple[int, int],
    parent_identities: tuple[tuple[int, int], ...],
    recorded_at: datetime | None = None,
) -> LiveApplyMutationGuard:
    """Persist exact directory identities after journaling and before mutation."""
    _validate_context(store, progress, plan)
    root = _identity(root_identity)
    parents = tuple(_identity(item) for item in parent_identities)
    when = _timestamp(recorded_at)
    parent_json = json.dumps(parents, separators=(",", ":"))
    values: tuple[object, ...] = (
        progress.deployment_id,
        progress.operation_index,
        progress.intent_record_sha256,
        progress.operations_sha256,
        progress.operation_path_sha256,
        root[0],
        root[1],
        parent_json,
        when.isoformat(),
    )
    record = LiveApplyMutationGuard(
        deployment_id=progress.deployment_id,
        operation_index=progress.operation_index,
        intent_record_sha256=progress.intent_record_sha256,
        operations_sha256=progress.operations_sha256,
        operation_path_sha256=progress.operation_path_sha256,
        root_identity=root,
        parent_identities=parents,
        recorded_at=when,
        record_sha256=_digest(values),
    )
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            row = _guard_row(db, record.deployment_id, record.operation_index)
            if row is not None:
                existing = _guard_from_row(row)
                if existing != record:
                    raise StateError("Live Apply mutation guard cannot be rebound")
                return existing
            db.execute(
                "INSERT INTO live_apply_mutation_guard VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, record.record_sha256),
            )
        loaded = load_live_apply_mutation_guard(store, record.deployment_id, record.operation_index)
        if loaded != record:
            raise StateError("Live Apply mutation guard was not persisted")
        return record
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to persist live Apply mutation guard") from None


def load_live_apply_mutation_guard(
    store: StateStore, deployment_id: str, operation_index: int
) -> LiveApplyMutationGuard | None:
    if type(store) is not StateStore:
        raise StateError("Invalid live Apply mutation guard store")
    try:
        row = _guard_row(store._connection, deployment_id, operation_index)
        return None if row is None else _guard_from_row(row)
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to read live Apply mutation guard") from None


def record_live_apply_reconciliation(
    store: StateStore,
    progress: LiveApplyProgress,
    *,
    plan: LiveApplyPlan,
    outcome: str,
    recorded_at: datetime | None = None,
) -> LiveApplyReconciliationRecord:
    """Persist one exact restart-safe, non-authoritative reconciliation result."""
    _validate_context(store, progress, plan)
    if outcome not in _OUTCOMES:
        raise StateError("Invalid live Apply reconciliation outcome")
    when = _timestamp(recorded_at)
    values: tuple[object, ...] = (
        progress.deployment_id,
        progress.operation_index,
        outcome,
        when.isoformat(),
    )
    record = LiveApplyReconciliationRecord(
        deployment_id=progress.deployment_id,
        operation_index=progress.operation_index,
        outcome=outcome,
        recorded_at=when,
        record_sha256=_digest(values),
    )
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            row = _reconciliation_row(db, progress.deployment_id, progress.operation_index)
            if row is not None:
                existing = _reconciliation_from_row(row)
                if existing.outcome != outcome:
                    raise StateError("Live Apply reconciliation outcome cannot be changed")
                return existing
            db.execute(
                "INSERT INTO live_apply_reconciliation VALUES (?, ?, ?, ?, ?)",
                (*values, record.record_sha256),
            )
        return record
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to persist live Apply reconciliation outcome") from None


def load_live_apply_reconciliation(
    store: StateStore, deployment_id: str, operation_index: int
) -> LiveApplyReconciliationRecord | None:
    if type(store) is not StateStore:
        raise StateError("Invalid live Apply reconciliation store")
    try:
        row = _reconciliation_row(store._connection, deployment_id, operation_index)
        return None if row is None else _reconciliation_from_row(row)
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to read live Apply reconciliation outcome") from None


def _validate_context(store: StateStore, progress: LiveApplyProgress, plan: LiveApplyPlan) -> None:
    if type(store) is not StateStore or type(progress) is not LiveApplyProgress:
        raise StateError("Invalid live Apply reconciliation evidence")
    try:
        validate_live_apply_progress_plan_binding(progress, plan)
    except Exception:
        raise StateError("Invalid live Apply reconciliation plan binding") from None
    persisted = load_live_apply_progress(store, progress.deployment_id, progress.operation_index)
    if persisted is None or persisted.progress != progress or persisted.phase != "mutation_started":
        raise StateError("Live Apply reconciliation requires mutation-started progress")


def _identity(value: object) -> tuple[int, int]:
    if (
        type(value) is not tuple
        or len(value) != 2
        or type(value[0]) is not int
        or type(value[1]) is not int
        or value[0] < 0
        or value[1] <= 0
    ):
        raise StateError("Invalid live Apply filesystem identity")
    return value


def _guard_row(
    db: sqlite3.Connection, deployment_id: str, operation_index: int
) -> tuple[object, ...] | None:
    row = db.execute(
        "SELECT deployment_id, operation_index, intent_record_sha256, operations_sha256, "
        "operation_path_sha256, root_device, root_inode, parent_identities_json, "
        "recorded_at, record_sha256 FROM live_apply_mutation_guard "
        "WHERE deployment_id = ? AND operation_index = ?",
        (deployment_id, operation_index),
    ).fetchone()
    return None if row is None else tuple(row)


def _reconciliation_row(
    db: sqlite3.Connection, deployment_id: str, operation_index: int
) -> tuple[object, ...] | None:
    row = db.execute(
        "SELECT deployment_id, operation_index, outcome, recorded_at, record_sha256 "
        "FROM live_apply_reconciliation WHERE deployment_id = ? AND operation_index = ?",
        (deployment_id, operation_index),
    ).fetchone()
    return None if row is None else tuple(row)


def _guard_from_row(row: tuple[object, ...]) -> LiveApplyMutationGuard:
    try:
        values: tuple[object, ...] = tuple(row)
        parents_raw = json.loads(_text(values[7]))
        parents = tuple(_identity(tuple(item)) for item in parents_raw)
        when = datetime.fromisoformat(_text(values[8]))
        record = LiveApplyMutationGuard(
            deployment_id=_text(values[0]),
            operation_index=_integer(values[1]),
            intent_record_sha256=_text(values[2]),
            operations_sha256=_text(values[3]),
            operation_path_sha256=_text(values[4]),
            root_identity=_identity((values[5], values[6])),
            parent_identities=parents,
            recorded_at=_timestamp(when),
            record_sha256=_text(values[9]),
        )
        digest_values = (*values[:7], json.dumps(parents, separators=(",", ":")), values[8])
        if (
            len(values) != 10
            or not all(isinstance(item, str) for item in values[0:1] + values[2:5])
            or _HASH.fullmatch(record.intent_record_sha256) is None
            or _HASH.fullmatch(record.operations_sha256) is None
            or _HASH.fullmatch(record.operation_path_sha256) is None
            or _HASH.fullmatch(record.record_sha256) is None
            or _digest(digest_values) != record.record_sha256
        ):
            raise ValueError
        return record
    except (TypeError, ValueError, IndexError, json.JSONDecodeError):
        raise StateError("Invalid live Apply mutation guard record") from None


def _reconciliation_from_row(row: tuple[object, ...]) -> LiveApplyReconciliationRecord:
    try:
        values: tuple[object, ...] = tuple(row)
        when = _timestamp(datetime.fromisoformat(_text(values[3])))
        record = LiveApplyReconciliationRecord(
            _text(values[0]),
            _integer(values[1]),
            _text(values[2]),
            when,
            _text(values[4]),
        )
        if (
            len(values) != 5
            or record.outcome not in _OUTCOMES
            or _HASH.fullmatch(record.record_sha256) is None
            or _digest(values[:4]) != record.record_sha256
        ):
            raise ValueError
        return record
    except (TypeError, ValueError, IndexError):
        raise StateError("Invalid live Apply reconciliation record") from None


def _timestamp(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise StateError("Live Apply reconciliation timestamp must include a timezone")
    return current.astimezone(UTC)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
