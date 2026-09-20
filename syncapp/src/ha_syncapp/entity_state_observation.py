"""Durable, content-free validity proof for exact affected entity states."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .integration_observation import SessionFactory
from .resource_availability_observation import (
    ResourceAvailabilityError,
    ResourceAvailabilityObservation,
    ResourceAvailabilityTarget,
    _probe_states,
    load_resource_availability_observation,
)
from .state import StateError, StateStore

_HASH = re.compile(r"^[0-9a-f]{64}$")
_INVALID_STATES = {"unknown", "unavailable"}


class EntityStateObservationError(RuntimeError):
    """Affected entity state validity could not be established safely."""


@dataclass(frozen=True, slots=True, init=False)
class EntityStateObservation:
    """Content-free aggregate bound to exact resource-availability evidence."""

    deployment_id: str
    resource_availability_sha256: str
    target_sha256: str
    observed_at: datetime
    expected_count: int
    valid_count: int
    invalid_count: int
    record_sha256: str

    @classmethod
    def create(
        cls,
        deployment_id: str,
        resource_availability_sha256: str,
        target_sha256: str,
        observed_at: datetime,
        expected_count: int,
        valid_count: int,
        invalid_count: int,
    ) -> EntityStateObservation:
        when = _timestamp(observed_at)
        values: tuple[object, ...] = (
            deployment_id,
            resource_availability_sha256,
            target_sha256,
            when.isoformat(),
            expected_count,
            valid_count,
            invalid_count,
        )
        result = object.__new__(cls)
        for name, value in zip(
            (
                "deployment_id",
                "resource_availability_sha256",
                "target_sha256",
                "observed_at",
                "expected_count",
                "valid_count",
                "invalid_count",
                "record_sha256",
            ),
            (*values[:3], when, *values[4:], _digest(values)),
            strict=True,
        ):
            object.__setattr__(result, name, value)
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> EntityStateObservation:
        if len(row) != 8:
            _invalid_state()
        try:
            when = datetime.fromisoformat(_text(row[3]))
        except ValueError:
            _invalid_state()
        result = object.__new__(cls)
        for name, value in zip(
            (
                "deployment_id",
                "resource_availability_sha256",
                "target_sha256",
                "observed_at",
                "expected_count",
                "valid_count",
                "invalid_count",
                "record_sha256",
            ),
            (
                _text(row[0]),
                _text(row[1]),
                _text(row[2]),
                when,
                row[4],
                row[5],
                row[6],
                _text(row[7]),
            ),
            strict=True,
        ):
            object.__setattr__(result, name, value)
        result._validate()
        if result.database_values() != row:
            _invalid_state()
        return result

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        values: tuple[object, ...] = (
            self.deployment_id,
            self.resource_availability_sha256,
            self.target_sha256,
            self.observed_at.astimezone(UTC).isoformat(),
            self.expected_count,
            self.valid_count,
            self.invalid_count,
        )
        return (*values, _digest(values))

    def _validate(self) -> None:
        counts = (self.expected_count, self.valid_count, self.invalid_count)
        if (
            not isinstance(self.deployment_id, str)
            or not self.deployment_id
            or _HASH.fullmatch(self.resource_availability_sha256) is None
            or _HASH.fullmatch(self.target_sha256) is None
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
            or any(type(value) is not int or value < 0 for value in counts)
            or self.valid_count + self.invalid_count != self.expected_count
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class EntityStateObservationResult:
    """Sanitized result, including durable deterministic invalidity."""

    status: str
    replayed: bool
    expected_count: int
    valid_count: int
    invalid_count: int


def observe_entity_states_once(
    store: StateStore,
    target: ResourceAvailabilityTarget,
    *,
    token: str | None = None,
    timeout_seconds: float = 10.0,
    max_message_bytes: int = 4 * 1024 * 1024,
    session_factory: SessionFactory | None = None,
    observed_at: datetime | None = None,
) -> EntityStateObservationResult:
    """Observe one exact state snapshot and durably classify the outcome."""
    availability = _prerequisite(store, target)
    existing = load_entity_state_observation(store, target)
    if existing is not None:
        return _result(existing, True)

    valid = 0
    invalid = 0
    if target.entity_ids:
        try:
            states = _probe_states(token, timeout_seconds, max_message_bytes, session_factory)
        except ResourceAvailabilityError:
            _unavailable()
        by_entity: dict[str, dict[str, object]] = {}
        for item in states:
            if type(item) is not dict:
                _unavailable()
            entity = item.get("entity_id")
            if isinstance(entity, str):
                by_entity[entity] = item
        if any(entity not in by_entity for entity in target.entity_ids):
            _unavailable()
        for entity in target.entity_ids:
            state = by_entity[entity].get("state")
            if type(state) is not str or not state:
                _unavailable()
            if state in _INVALID_STATES:
                invalid += 1
            else:
                valid += 1

    requested = EntityStateObservation.create(
        target.deployment_id,
        availability.record_sha256,
        target.target_sha256,
        _timestamp(observed_at),
        len(target.entity_ids),
        valid,
        invalid,
    )
    if requested.observed_at < availability.observed_at:
        _invalid_state()
    return _record(store, target, requested)


def load_entity_state_observation(
    store: StateStore, target: ResourceAvailabilityTarget
) -> EntityStateObservation | None:
    try:
        availability = _prerequisite(store, target)
        rows = store._connection.execute(
            "SELECT deployment_id, resource_availability_sha256, target_sha256, "
            "observed_at, expected_count, valid_count, invalid_count, record_sha256 "
            "FROM entity_state_observation WHERE deployment_id = ?",
            (target.deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = EntityStateObservation.from_database_row(tuple(rows[0]))
        if (
            result.resource_availability_sha256 != availability.record_sha256
            or result.target_sha256 != target.target_sha256
            or result.expected_count != len(target.entity_ids)
            or result.observed_at < availability.observed_at
        ):
            _invalid_state()
        return result
    except EntityStateObservationError:
        raise
    except (ResourceAvailabilityError, StateError, sqlite3.Error, AttributeError):
        _invalid_state()


def _record(
    store: StateStore,
    target: ResourceAvailabilityTarget,
    requested: EntityStateObservation,
) -> EntityStateObservationResult:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            availability = _prerequisite(store, target)
            if (
                requested.resource_availability_sha256 != availability.record_sha256
                or requested.target_sha256 != target.target_sha256
                or requested.observed_at < availability.observed_at
            ):
                _invalid_state()
            existing = load_entity_state_observation(store, target)
            if existing is not None:
                return _result(existing, True)
            db.execute(
                "INSERT INTO entity_state_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_entity_state_observation(store, target)
        if loaded != requested:
            _invalid_state()
        return _result(requested, False)
    except EntityStateObservationError:
        raise
    except (ResourceAvailabilityError, StateError, sqlite3.Error):
        _invalid_state()


def _prerequisite(
    store: StateStore, target: ResourceAvailabilityTarget
) -> ResourceAvailabilityObservation:
    try:
        availability = load_resource_availability_observation(store, target)
    except ResourceAvailabilityError:
        _invalid_state()
    if availability is None:
        raise EntityStateObservationError("Exact resource availability proof is required")
    return availability


def _result(observation: EntityStateObservation, replayed: bool) -> EntityStateObservationResult:
    status = "valid" if observation.invalid_count == 0 else "invalid_states"
    return EntityStateObservationResult(
        status,
        replayed,
        observation.expected_count,
        observation.valid_count,
        observation.invalid_count,
    )


def _timestamp(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        _invalid_state()
    return current.astimezone(UTC)


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid_state()
    return value


def _invalid_state() -> NoReturn:
    raise EntityStateObservationError("entity state observation state is invalid") from None


def _unavailable() -> NoReturn:
    raise EntityStateObservationError("entity state observation is unavailable") from None
