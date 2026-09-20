"""Durable proof that exact affected automations and scripts loaded."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .entity_state_observation import (
    EntityStateObservation,
    EntityStateObservationError,
    load_entity_state_observation,
)
from .integration_observation import SessionFactory
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .resource_availability_observation import (
    ResourceAvailabilityError,
    ResourceAvailabilityTarget,
    _probe_states,
)
from .state import StateError, StateStore

_HASH = re.compile(r"^[0-9a-f]{64}$")
_LOADED_STATES = {"on", "off"}


class AutomationScriptObservationError(RuntimeError):
    """Automation/script loading could not be proved safely."""


@dataclass(frozen=True, slots=True, init=False)
class AutomationScriptTarget:
    """Relevant subset deterministically derived from one resource target."""

    resource_target: ResourceAvailabilityTarget
    entity_ids: tuple[str, ...]
    target_sha256: str

    def _validate(self) -> None:
        try:
            self.resource_target._validate()
        except ResourceAvailabilityError:
            _invalid_state()
        expected = tuple(
            entity
            for entity in self.resource_target.entity_ids
            if entity.startswith(("automation.", "script."))
        )
        values = (self.resource_target.target_sha256, expected)
        if (
            self.entity_ids != expected
            or _HASH.fullmatch(self.target_sha256) is None
            or self.target_sha256 != _digest(values)
        ):
            _invalid_state()


def derive_automation_script_target(
    resource_target: ResourceAvailabilityTarget,
) -> AutomationScriptTarget:
    """Derive relevant resources without accepting caller-selected identifiers."""
    try:
        resource_target._validate()
    except ResourceAvailabilityError:
        _invalid_state()
    entities = tuple(
        entity
        for entity in resource_target.entity_ids
        if entity.startswith(("automation.", "script."))
    )
    result = object.__new__(AutomationScriptTarget)
    object.__setattr__(result, "resource_target", resource_target)
    object.__setattr__(result, "entity_ids", entities)
    object.__setattr__(result, "target_sha256", _digest((resource_target.target_sha256, entities)))
    result._validate()
    return result


@dataclass(frozen=True, slots=True, init=False)
class AutomationScriptObservation:
    deployment_id: str
    entity_state_observation_sha256: str
    target_sha256: str
    observed_at: datetime
    expected_count: int
    loaded_count: int
    failed_count: int
    record_sha256: str

    @classmethod
    def create(
        cls,
        deployment_id: str,
        entity_state_observation_sha256: str,
        target_sha256: str,
        observed_at: datetime,
        expected_count: int,
        loaded_count: int,
        failed_count: int,
    ) -> AutomationScriptObservation:
        when = _timestamp(observed_at)
        values: tuple[object, ...] = (
            deployment_id,
            entity_state_observation_sha256,
            target_sha256,
            when.isoformat(),
            expected_count,
            loaded_count,
            failed_count,
        )
        result = object.__new__(cls)
        for name, value in zip(
            (
                "deployment_id",
                "entity_state_observation_sha256",
                "target_sha256",
                "observed_at",
                "expected_count",
                "loaded_count",
                "failed_count",
                "record_sha256",
            ),
            (*values[:3], when, *values[4:], _digest(values)),
            strict=True,
        ):
            object.__setattr__(result, name, value)
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> AutomationScriptObservation:
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
                "entity_state_observation_sha256",
                "target_sha256",
                "observed_at",
                "expected_count",
                "loaded_count",
                "failed_count",
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
            self.entity_state_observation_sha256,
            self.target_sha256,
            self.observed_at.astimezone(UTC).isoformat(),
            self.expected_count,
            self.loaded_count,
            self.failed_count,
        )
        return (*values, _digest(values))

    def _validate(self) -> None:
        counts = (self.expected_count, self.loaded_count, self.failed_count)
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        if (
            _HASH.fullmatch(self.entity_state_observation_sha256) is None
            or _HASH.fullmatch(self.target_sha256) is None
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
            or any(type(value) is not int or value < 0 for value in counts)
            or self.loaded_count + self.failed_count != self.expected_count
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class AutomationScriptObservationResult:
    status: str
    replayed: bool
    expected_count: int
    loaded_count: int
    failed_count: int


def observe_automation_scripts_once(
    store: StateStore,
    target: AutomationScriptTarget,
    *,
    token: str | None = None,
    timeout_seconds: float = 10.0,
    max_message_bytes: int = 4 * 1024 * 1024,
    session_factory: SessionFactory | None = None,
    observed_at: datetime | None = None,
) -> AutomationScriptObservationResult:
    target._validate()
    entity_state = _prerequisite(store, target)
    existing = load_automation_script_observation(store, target)
    if existing is not None:
        return _result(existing, True)

    loaded = 0
    failed = 0
    if target.entity_ids:
        try:
            states = _probe_states(token, timeout_seconds, max_message_bytes, session_factory)
        except ResourceAvailabilityError:
            _unavailable()
        for entity in target.entity_ids:
            matches = [
                item for item in states if type(item) is dict and item.get("entity_id") == entity
            ]
            if len(matches) == 1 and matches[0].get("state") in _LOADED_STATES:
                loaded += 1
            else:
                failed += 1

    requested = AutomationScriptObservation.create(
        target.resource_target.deployment_id,
        entity_state.record_sha256,
        target.target_sha256,
        _timestamp(observed_at),
        len(target.entity_ids),
        loaded,
        failed,
    )
    if requested.observed_at < entity_state.observed_at:
        _invalid_state()
    return _record(store, target, requested)


def load_automation_script_observation(
    store: StateStore, target: AutomationScriptTarget
) -> AutomationScriptObservation | None:
    try:
        target._validate()
        entity_state = _prerequisite(store, target)
        rows = store._connection.execute(
            "SELECT deployment_id, entity_state_observation_sha256, target_sha256, "
            "observed_at, expected_count, loaded_count, failed_count, record_sha256 "
            "FROM automation_script_observation WHERE deployment_id = ?",
            (target.resource_target.deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = AutomationScriptObservation.from_database_row(tuple(rows[0]))
        if (
            result.entity_state_observation_sha256 != entity_state.record_sha256
            or result.target_sha256 != target.target_sha256
            or result.expected_count != len(target.entity_ids)
            or result.observed_at < entity_state.observed_at
        ):
            _invalid_state()
        return result
    except AutomationScriptObservationError:
        raise
    except (EntityStateObservationError, ResourceAvailabilityError, StateError, sqlite3.Error):
        _invalid_state()


def _record(
    store: StateStore, target: AutomationScriptTarget, requested: AutomationScriptObservation
) -> AutomationScriptObservationResult:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            entity_state = _prerequisite(store, target)
            if (
                requested.entity_state_observation_sha256 != entity_state.record_sha256
                or requested.target_sha256 != target.target_sha256
                or requested.observed_at < entity_state.observed_at
            ):
                _invalid_state()
            existing = load_automation_script_observation(store, target)
            if existing is not None:
                return _result(existing, True)
            db.execute(
                "INSERT INTO automation_script_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_automation_script_observation(store, target)
        if loaded != requested:
            _invalid_state()
        return _result(requested, False)
    except AutomationScriptObservationError:
        raise
    except (EntityStateObservationError, ResourceAvailabilityError, StateError, sqlite3.Error):
        _invalid_state()


def _prerequisite(store: StateStore, target: AutomationScriptTarget) -> EntityStateObservation:
    try:
        observation = load_entity_state_observation(store, target.resource_target)
    except EntityStateObservationError:
        _invalid_state()
    if observation is None or observation.invalid_count != 0:
        raise AutomationScriptObservationError("Successful affected-entity state proof is required")
    return observation


def _result(
    value: AutomationScriptObservation, replayed: bool
) -> AutomationScriptObservationResult:
    status = "loaded" if value.failed_count == 0 else "load_failed"
    return AutomationScriptObservationResult(
        status, replayed, value.expected_count, value.loaded_count, value.failed_count
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
    raise AutomationScriptObservationError(
        "automation/script observation state is invalid"
    ) from None


def _unavailable() -> NoReturn:
    raise AutomationScriptObservationError("automation/script observation is unavailable") from None
