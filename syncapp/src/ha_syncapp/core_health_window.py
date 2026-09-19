"""Durable configurable two-point Core API health observation window."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from .core_health_observation import (
    CoreHealthError,
    CoreHealthObservation,
    CoreHealthTransport,
    load_core_health_observation,
    probe_core_api_health,
)
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .state import StateError, StateStore

_HASH = re.compile(r"^[0-9a-f]{64}$")
_MIN_OBSERVATION_SECONDS = 30
_MAX_OBSERVATION_SECONDS = 3600


class CoreHealthWindowError(RuntimeError):
    """The bounded Core health observation window cannot advance safely."""


@dataclass(frozen=True, slots=True, init=False)
class CoreHealthWindow:
    """Integrity-protected state for one deployment observation interval."""

    deployment_id: str
    initial_health_sha256: str
    started_at: datetime
    deadline_at: datetime
    completed_at: datetime | None
    record_sha256: str

    @classmethod
    def create(
        cls,
        deployment_id: str,
        initial_health_sha256: str,
        started_at: datetime,
        deadline_at: datetime,
        completed_at: datetime | None = None,
    ) -> CoreHealthWindow:
        start = _timestamp(started_at)
        deadline = _timestamp(deadline_at)
        completed = None if completed_at is None else _timestamp(completed_at)
        values = _values(deployment_id, initial_health_sha256, start, deadline, completed)
        result = _construct_window(
            (
                deployment_id,
                initial_health_sha256,
                start,
                deadline,
                completed,
                _record_digest(values),
            )
        )
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CoreHealthWindow:
        if len(row) != 6:
            _invalid_state()
        try:
            started_at = datetime.fromisoformat(_text(row[2]))
            deadline_at = datetime.fromisoformat(_text(row[3]))
            completed_at = None if row[4] is None else datetime.fromisoformat(_text(row[4]))
        except ValueError:
            _invalid_state()
        result = _construct_window(
            (
                _text(row[0]),
                _text(row[1]),
                started_at,
                deadline_at,
                completed_at,
                _text(row[5]),
            )
        )
        result._validate()
        if result.database_values() != row:
            _invalid_state()
        return result

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        values = _values(
            self.deployment_id,
            self.initial_health_sha256,
            self.started_at,
            self.deadline_at,
            self.completed_at,
        )
        return (*values, _record_digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        if (
            _HASH.fullmatch(self.initial_health_sha256) is None
            or not _aware(self.started_at)
            or not _aware(self.deadline_at)
            or self.deadline_at <= self.started_at
            or (self.completed_at is not None and not _aware(self.completed_at))
            or (self.completed_at is not None and self.completed_at < self.deadline_at)
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class CoreHealthWindowResult:
    """Sanitized outcome from one non-blocking window advancement."""

    status: str
    replayed: bool
    deadline_at: datetime


def advance_core_health_window_once(
    store: StateStore,
    deployment_id: str,
    *,
    observation_seconds: int,
    token: str | None = None,
    timeout_seconds: float = 10.0,
    max_response_bytes: int = 16 * 1024,
    transport: CoreHealthTransport | None = None,
    now: datetime | None = None,
) -> CoreHealthWindowResult:
    """Start, wait without sleeping, or complete one exact health window."""
    _validate_duration(observation_seconds)
    when = _timestamp(now)
    initial = _initial_health(store, deployment_id)
    existing = load_core_health_window(store, deployment_id)
    if existing is None:
        if when < initial.observed_at:
            _invalid_state()
        window, inserted = _record_started(
            store,
            CoreHealthWindow.create(
                deployment_id,
                initial.record_sha256,
                when,
                when + timedelta(seconds=observation_seconds),
            ),
        )
        if inserted:
            return CoreHealthWindowResult("observing", False, window.deadline_at)
        existing = window

    if existing.completed_at is not None:
        return CoreHealthWindowResult("healthy", True, existing.deadline_at)
    if when < existing.started_at:
        _invalid_state()
    if when < existing.deadline_at:
        return CoreHealthWindowResult("observing", True, existing.deadline_at)

    try:
        probe_core_api_health(
            token=token,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            transport=transport,
        )
    except CoreHealthError:
        raise CoreHealthWindowError("Core health is unavailable") from None
    completed, replayed = _record_completed(store, existing, when)
    return CoreHealthWindowResult("healthy", replayed, completed.deadline_at)


def load_core_health_window(store: StateStore, deployment_id: str) -> CoreHealthWindow | None:
    """Load a window while revalidating its exact initial health proof."""
    if type(store) is not StateStore:
        _invalid_state()
    try:
        validate_deployment_id(deployment_id)
        rows = store._connection.execute(
            "SELECT deployment_id, initial_health_sha256, started_at, deadline_at, "
            "completed_at, record_sha256 FROM core_health_window WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = CoreHealthWindow.from_database_row(tuple(rows[0]))
        initial = _initial_health(store, deployment_id)
        if (
            result.initial_health_sha256 != initial.record_sha256
            or result.started_at < initial.observed_at
        ):
            _invalid_state()
        return result
    except CoreHealthWindowError:
        raise
    except (PreparedDeploymentError, CoreHealthError, StateError, sqlite3.Error):
        _invalid_state()


def _record_started(
    store: StateStore, requested: CoreHealthWindow
) -> tuple[CoreHealthWindow, bool]:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            initial = _initial_health(store, requested.deployment_id)
            if (
                initial.record_sha256 != requested.initial_health_sha256
                or requested.started_at < initial.observed_at
            ):
                _invalid_state()
            existing = load_core_health_window(store, requested.deployment_id)
            if existing is not None:
                return existing, False
            db.execute(
                "INSERT INTO core_health_window (deployment_id, initial_health_sha256, "
                "started_at, deadline_at, completed_at, record_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_core_health_window(store, requested.deployment_id)
        if loaded != requested:
            _invalid_state()
        return requested, True
    except CoreHealthWindowError:
        raise
    except (CoreHealthError, StateError, sqlite3.Error):
        _invalid_state()


def _record_completed(
    store: StateStore, existing: CoreHealthWindow, when: datetime
) -> tuple[CoreHealthWindow, bool]:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            current = load_core_health_window(store, existing.deployment_id)
            if current is None:
                _invalid_state()
            if current.completed_at is not None:
                return current, True
            if current != existing or when < current.deadline_at:
                _invalid_state()
            replacement = CoreHealthWindow.create(
                current.deployment_id,
                current.initial_health_sha256,
                current.started_at,
                current.deadline_at,
                when,
            )
            updated = db.execute(
                "UPDATE core_health_window SET completed_at = ?, record_sha256 = ? "
                "WHERE deployment_id = ? AND completed_at IS NULL AND record_sha256 = ?",
                (
                    when.isoformat(),
                    replacement.record_sha256,
                    replacement.deployment_id,
                    current.record_sha256,
                ),
            )
            if updated.rowcount != 1:
                _invalid_state()
        loaded = load_core_health_window(store, existing.deployment_id)
        if loaded != replacement:
            _invalid_state()
        return replacement, False
    except CoreHealthWindowError:
        raise
    except (CoreHealthError, StateError, sqlite3.Error):
        _invalid_state()


def _initial_health(store: StateStore, deployment_id: str) -> CoreHealthObservation:
    try:
        initial = load_core_health_observation(store, deployment_id)
    except CoreHealthError:
        _invalid_state()
    if initial is None:
        raise CoreHealthWindowError("Core initial health is unavailable")
    return initial


def _validate_duration(value: int) -> None:
    if type(value) is not int or not _MIN_OBSERVATION_SECONDS <= value <= _MAX_OBSERVATION_SECONDS:
        raise CoreHealthWindowError("Core health observation duration is invalid")


def _values(
    deployment_id: str,
    initial_health_sha256: str,
    started_at: datetime,
    deadline_at: datetime,
    completed_at: datetime | None,
) -> tuple[object, ...]:
    return (
        deployment_id,
        initial_health_sha256,
        started_at.astimezone(UTC).isoformat(),
        deadline_at.astimezone(UTC).isoformat(),
        None if completed_at is None else completed_at.astimezone(UTC).isoformat(),
    )


def _record_digest(values: tuple[object, ...]) -> str:
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _construct_window(values: tuple[object, ...]) -> CoreHealthWindow:
    if len(values) != 6:
        _invalid_state()
    result = object.__new__(CoreHealthWindow)
    for name, value in zip(CoreHealthWindow.__slots__, values, strict=True):
        object.__setattr__(result, name, value)
    return result


def _timestamp(value: datetime | None = None) -> datetime:
    when = datetime.now(UTC) if value is None else value
    if not _aware(when):
        raise CoreHealthWindowError("Core health observation timestamp is invalid")
    return when.astimezone(UTC)


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid_state()
    return value


def _invalid_state() -> NoReturn:
    raise CoreHealthWindowError("Core health window state is invalid") from None
