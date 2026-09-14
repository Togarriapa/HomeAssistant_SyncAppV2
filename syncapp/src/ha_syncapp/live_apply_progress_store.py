"""Crash-safe, non-authoritative persistence for ordered live Apply progress."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .live_apply_intent_store import load_live_apply_intent
from .live_apply_plan import LiveApplyPlan
from .live_apply_progress import (
    LiveApplyProgress,
    LiveApplyProgressError,
    live_apply_plan_operations_sha256,
    transition_live_apply_progress,
    validate_live_apply_progress_plan_binding,
)
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .state import StateError, StateStore

_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_DISCOVERABLE_PROGRESS = 4096


@dataclass(frozen=True, slots=True)
class LiveApplyRecoveryDecision:
    """Deterministic, non-authoritative restart/retrigger recovery classification."""

    action: str
    operation_index: int | None
    operation_path_sha256: str | None


@dataclass(frozen=True, slots=True)
class PersistedLiveApplyProgress:
    """Durable content-free evidence for one ordered live Apply operation."""

    deployment_id: str
    operation_index: int
    intent_record_sha256: str
    operations_sha256: str
    operation_path_sha256: str
    phase: str
    updated_at: datetime
    record_sha256: str

    @classmethod
    def from_progress(
        cls,
        progress: LiveApplyProgress,
        updated_at: datetime,
    ) -> PersistedLiveApplyProgress:
        if type(progress) is not LiveApplyProgress:
            raise StateError("Invalid live Apply progress evidence")
        when = _timestamp(updated_at)
        values: tuple[object, ...] = (
            progress.deployment_id,
            progress.operation_index,
            progress.intent_record_sha256,
            progress.operations_sha256,
            progress.operation_path_sha256,
            progress.phase,
            when.isoformat(),
        )
        return cls(
            deployment_id=progress.deployment_id,
            operation_index=progress.operation_index,
            intent_record_sha256=progress.intent_record_sha256,
            operations_sha256=progress.operations_sha256,
            operation_path_sha256=progress.operation_path_sha256,
            phase=progress.phase,
            updated_at=when,
            record_sha256=_record_digest(values),
        )

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> PersistedLiveApplyProgress:
        if len(row) != 8:
            _invalid_record()
        deployment_id = _text(row[0])
        operation_index = row[1]
        intent_record_sha256 = _text(row[2])
        operations_sha256 = _text(row[3])
        operation_path_sha256 = _text(row[4])
        phase = _text(row[5])
        updated_at_text = _text(row[6])
        record_sha256 = _text(row[7])
        if type(operation_index) is not int:
            _invalid_record()
        try:
            updated_at = datetime.fromisoformat(updated_at_text)
        except ValueError:
            _invalid_record()
        record = cls(
            deployment_id=deployment_id,
            operation_index=operation_index,
            intent_record_sha256=intent_record_sha256,
            operations_sha256=operations_sha256,
            operation_path_sha256=operation_path_sha256,
            phase=phase,
            updated_at=updated_at,
            record_sha256=record_sha256,
        )
        record._validate()
        if record.database_values() != row:
            _invalid_record()
        return record

    @property
    def progress(self) -> LiveApplyProgress:
        """Reconstruct validated in-memory evidence without granting mutation authority."""
        self._validate()
        try:
            return LiveApplyProgress.create(
                deployment_id=self.deployment_id,
                intent_record_sha256=self.intent_record_sha256,
                operations_sha256=self.operations_sha256,
                operation_index=self.operation_index,
                operation_path_sha256=self.operation_path_sha256,
                phase=self.phase,
            )
        except LiveApplyProgressError:
            _invalid_record()

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        values: tuple[object, ...] = (
            self.deployment_id,
            self.operation_index,
            self.intent_record_sha256,
            self.operations_sha256,
            self.operation_path_sha256,
            self.phase,
            self.updated_at.astimezone(UTC).isoformat(),
        )
        return (*values, _record_digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_record()
        if (
            type(self.operation_index) is not int
            or self.operation_index < 0
            or _HASH.fullmatch(self.intent_record_sha256) is None
            or _HASH.fullmatch(self.operations_sha256) is None
            or _HASH.fullmatch(self.operation_path_sha256) is None
            or self.phase not in {"mutation_started", "mutation_verified", "blocked"}
            or not isinstance(self.updated_at, datetime)
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_record()


def record_live_apply_progress(
    store: StateStore,
    progress: LiveApplyProgress,
    *,
    plan: LiveApplyPlan,
    updated_at: datetime | None = None,
) -> PersistedLiveApplyProgress:
    """Durably record one plan-proven monotonic transition before/after mutation."""
    if type(store) is not StateStore:
        raise StateError("Invalid live Apply progress store")
    if type(progress) is not LiveApplyProgress:
        raise StateError("Invalid live Apply progress evidence")
    _revalidate_plan_binding(store, progress, plan)
    when = _timestamp(updated_at)
    requested = PersistedLiveApplyProgress.from_progress(progress, when)
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _revalidate_plan_binding(store, progress, plan)
            existing_row = _select_progress_row(
                db, progress.deployment_id, progress.operation_index
            )
            if existing_row is not None:
                existing = _parse_and_revalidate(store, existing_row)
                if not _same_operation(existing, requested):
                    raise StateError("Live Apply progress cannot be rebound")
                if existing.phase == requested.phase:
                    return existing
                try:
                    transitioned = transition_live_apply_progress(
                        existing.progress, requested.phase
                    )
                except LiveApplyProgressError as error:
                    raise StateError(str(error)) from None
                replacement = PersistedLiveApplyProgress.from_progress(transitioned, when)
                result = db.execute(
                    "UPDATE live_apply_progress SET phase = ?, updated_at = ?, record_sha256 = ? "
                    "WHERE deployment_id = ? AND operation_index = ? AND record_sha256 = ?",
                    (
                        replacement.phase,
                        replacement.updated_at.isoformat(),
                        replacement.record_sha256,
                        replacement.deployment_id,
                        replacement.operation_index,
                        existing.record_sha256,
                    ),
                )
                if result.rowcount != 1:
                    raise StateError("Live Apply progress changed unexpectedly")
                return replacement

            if progress.phase != "mutation_started":
                raise StateError("New live Apply progress must start before mutation")
            prior = _select_progress_rows(db, progress.deployment_id)
            parsed_prior = tuple(_parse_and_revalidate(store, row) for row in prior)
            _validate_contiguous_history(parsed_prior)
            if progress.operation_index != len(parsed_prior):
                raise StateError("Live Apply progress operations must be contiguous")
            if parsed_prior and parsed_prior[-1].phase != "mutation_verified":
                raise StateError("Live Apply progress previous operation is not verified")
            try:
                db.execute(
                    "INSERT INTO live_apply_progress (deployment_id, operation_index, "
                    "intent_record_sha256, operations_sha256, operation_path_sha256, phase, "
                    "updated_at, record_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    requested.database_values(),
                )
            except sqlite3.IntegrityError:
                raise StateError("Live Apply progress cannot be rebound") from None
        loaded = load_live_apply_progress(store, progress.deployment_id, progress.operation_index)
        if loaded is None or loaded != requested:
            raise StateError("Live Apply progress was not persisted")
        return loaded
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to persist live Apply progress") from None


def load_live_apply_progress(
    store: StateStore,
    deployment_id: str,
    operation_index: int,
) -> PersistedLiveApplyProgress | None:
    """Load one progress record while rechecking intent and integrity bindings."""
    if type(store) is not StateStore:
        raise StateError("Invalid live Apply progress store")
    _validate_identity(deployment_id, operation_index)
    try:
        row = _select_progress_row(store._connection, deployment_id, operation_index)
        if row is None:
            return None
        return _parse_and_revalidate(store, row)
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to read live Apply progress") from None


def discover_live_apply_progress(
    store: StateStore,
    deployment_id: str,
) -> tuple[PersistedLiveApplyProgress, ...]:
    """Discover bounded ordered progress for deterministic restart/retrigger recovery."""
    if type(store) is not StateStore:
        raise StateError("Invalid live Apply progress store")
    _validate_identity(deployment_id, 0)
    try:
        rows = store._connection.execute(
            "SELECT deployment_id, operation_index, intent_record_sha256, operations_sha256, "
            "operation_path_sha256, phase, updated_at, record_sha256 "
            "FROM live_apply_progress WHERE deployment_id = ? ORDER BY operation_index LIMIT ?",
            (deployment_id, _MAX_DISCOVERABLE_PROGRESS + 1),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE_PROGRESS:
            raise StateError("Live Apply progress discovery exceeds the limit")
        result = tuple(_parse_and_revalidate(store, tuple(row)) for row in rows)
        _validate_contiguous_history(result)
        return result
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to discover live Apply progress") from None


def discover_live_apply_recovery(
    store: StateStore,
    plan: LiveApplyPlan,
) -> LiveApplyRecoveryDecision:
    """Classify the only safe restart/retrigger action without granting write authority."""
    intent = _revalidate_plan_identity(store, plan)
    records = discover_live_apply_progress(store, intent.deployment_id)
    for record in records:
        _revalidate_plan_binding(store, record.progress, plan)

    operation_count = len(plan.operations)
    if len(records) > operation_count:
        _invalid_record()
    if not records:
        if operation_count == 0:
            return LiveApplyRecoveryDecision("complete", None, None)
        return LiveApplyRecoveryDecision(
            "start_next",
            0,
            hashlib.sha256(plan.operations[0].path.encode("utf-8")).hexdigest(),
        )

    current = records[-1]
    if current.phase == "mutation_started":
        return LiveApplyRecoveryDecision(
            "reconcile_uncertain",
            current.operation_index,
            current.operation_path_sha256,
        )
    if current.phase == "blocked":
        return LiveApplyRecoveryDecision(
            "blocked",
            current.operation_index,
            current.operation_path_sha256,
        )
    if len(records) == operation_count:
        return LiveApplyRecoveryDecision("complete", None, None)

    next_index = len(records)
    return LiveApplyRecoveryDecision(
        "start_next",
        next_index,
        hashlib.sha256(plan.operations[next_index].path.encode("utf-8")).hexdigest(),
    )


def _revalidate_plan_identity(store: StateStore, plan: LiveApplyPlan):
    if type(store) is not StateStore:
        raise StateError("Invalid live Apply progress store")
    if type(plan) is not LiveApplyPlan:
        raise StateError("Invalid live Apply progress Apply plan")
    intent = load_live_apply_intent(store, plan.deployment_id)
    if intent is None:
        raise StateError("Live Apply progress requires matching durable intent")
    if (
        plan.deployment_id,
        plan.target,
        plan.repository_id,
        plan.baseline_sha,
        plan.candidate_sha,
        plan.stage_manifest_sha256,
    ) != (
        intent.deployment_id,
        intent.target,
        intent.repository_id,
        intent.baseline_sha,
        intent.candidate_sha,
        intent.stage_manifest_sha256,
    ):
        raise StateError("Live Apply progress Apply plan binding mismatch")
    try:
        operations_sha256 = live_apply_plan_operations_sha256(plan)
    except LiveApplyProgressError as error:
        raise StateError(str(error)) from None
    if operations_sha256 != intent.operations_sha256:
        raise StateError("Live Apply progress durable intent binding mismatch")
    return intent


def _revalidate_plan_binding(
    store: StateStore,
    progress: LiveApplyProgress,
    plan: LiveApplyPlan,
) -> None:
    intent = _revalidate_plan_identity(store, plan)
    if intent.deployment_id != progress.deployment_id:
        raise StateError("Live Apply progress durable intent binding mismatch")
    if (
        intent.record_sha256 != progress.intent_record_sha256
        or intent.operations_sha256 != progress.operations_sha256
    ):
        raise StateError("Live Apply progress durable intent binding mismatch")
    try:
        validate_live_apply_progress_plan_binding(progress, plan)
    except LiveApplyProgressError as error:
        raise StateError(str(error)) from None


def _revalidate_intent_binding(store: StateStore, progress: LiveApplyProgress) -> None:
    intent = load_live_apply_intent(store, progress.deployment_id)
    if intent is None:
        raise StateError("Live Apply progress requires matching durable intent")
    if (
        intent.record_sha256 != progress.intent_record_sha256
        or intent.operations_sha256 != progress.operations_sha256
    ):
        raise StateError("Live Apply progress durable intent binding mismatch")


def _parse_and_revalidate(
    store: StateStore,
    row: tuple[object, ...],
) -> PersistedLiveApplyProgress:
    record = PersistedLiveApplyProgress.from_database_row(row)
    _revalidate_intent_binding(store, record.progress)
    return record


def _same_operation(
    left: PersistedLiveApplyProgress,
    right: PersistedLiveApplyProgress,
) -> bool:
    return astuple(left)[:5] == astuple(right)[:5]


def _validate_contiguous_history(records: tuple[PersistedLiveApplyProgress, ...]) -> None:
    for expected_index, record in enumerate(records):
        if record.operation_index != expected_index:
            _invalid_record()
        if expected_index < len(records) - 1 and record.phase != "mutation_verified":
            _invalid_record()


def _select_progress_row(
    db: sqlite3.Connection,
    deployment_id: str,
    operation_index: int,
) -> tuple[object, ...] | None:
    rows = db.execute(
        "SELECT deployment_id, operation_index, intent_record_sha256, operations_sha256, "
        "operation_path_sha256, phase, updated_at, record_sha256 FROM live_apply_progress "
        "WHERE deployment_id = ? AND operation_index = ?",
        (deployment_id, operation_index),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        _invalid_record()
    return tuple(rows[0])


def _select_progress_rows(
    db: sqlite3.Connection,
    deployment_id: str,
) -> tuple[tuple[object, ...], ...]:
    rows = db.execute(
        "SELECT deployment_id, operation_index, intent_record_sha256, operations_sha256, "
        "operation_path_sha256, phase, updated_at, record_sha256 FROM live_apply_progress "
        "WHERE deployment_id = ? ORDER BY operation_index",
        (deployment_id,),
    ).fetchall()
    if len(rows) > _MAX_DISCOVERABLE_PROGRESS:
        raise StateError("Live Apply progress discovery exceeds the limit")
    return tuple(tuple(row) for row in rows)


def _validate_identity(deployment_id: str, operation_index: int) -> None:
    try:
        validate_deployment_id(deployment_id)
    except PreparedDeploymentError:
        raise StateError("Invalid live Apply progress identity") from None
    if type(operation_index) is not int or operation_index < 0:
        raise StateError("Invalid live Apply progress identity")


def _timestamp(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise StateError("Live Apply progress timestamp must include a timezone")
    return current.astimezone(UTC)


def _record_digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid_record()
    return value


def _invalid_record() -> NoReturn:
    raise StateError("Invalid live Apply progress record") from None
