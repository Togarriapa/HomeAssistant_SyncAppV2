"""Content-free proof that exact changed Home Assistant resources are present."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, NoReturn

from websockets.sync.client import connect

from .candidate_dependencies import CandidateDependencyAnalysis
from .candidate_impact import CandidateImpactAnalysis
from .candidate_risk import (
    CandidateRiskClassification,
    CandidateRiskError,
    verify_candidate_risk_classification,
)
from .integration_observation import SessionFactory, WebSocketSession
from .prepared_deployment import PreparedDeployment, PreparedDeploymentError, validate_deployment_id
from .runtime_inventory import RuntimeInventoryInput
from .startup_error_observation import (
    StartupErrorObservation,
    StartupErrorObservationError,
    load_startup_error_observation,
)
from .state import StateError, StateStore

_CORE_WEBSOCKET_URL: Final = "ws://supervisor/core/websocket"
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_MESSAGE_BYTES: Final = 4 * 1024 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class ResourceAvailabilityError(RuntimeError):
    """Changed-resource availability could not be proved safely."""


@dataclass(frozen=True, slots=True, init=False)
class ResourceAvailabilityTarget:
    """Exact, integrity-bound resource set derived from trusted candidate evidence."""

    deployment_id: str
    candidate_sha: str
    entity_ids: tuple[str, ...]
    target_sha256: str

    @classmethod
    def create(
        cls, prepared: PreparedDeployment, entity_ids: tuple[str, ...]
    ) -> ResourceAvailabilityTarget:
        try:
            prepared.validate()
        except (AttributeError, PreparedDeploymentError):
            _invalid_state()
        entities = _canonical_entities(entity_ids)
        values = (prepared.deployment_id, prepared.evidence.candidate_sha, entities)
        result = object.__new__(cls)
        for name, value in zip(
            ("deployment_id", "candidate_sha", "entity_ids", "target_sha256"),
            (*values, _digest(values)),
            strict=True,
        ):
            object.__setattr__(result, name, value)
        result._validate()
        return result

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        values = (self.deployment_id, self.candidate_sha, self.entity_ids)
        if (
            re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.candidate_sha) is None
            or _canonical_entities(self.entity_ids) != self.entity_ids
            or _HASH.fullmatch(self.target_sha256) is None
            or self.target_sha256 != _digest(values)
        ):
            _invalid_state()


def derive_resource_availability_target(
    prepared: PreparedDeployment,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    risk: CandidateRiskClassification,
    runtime: RuntimeInventoryInput,
) -> ResourceAvailabilityTarget:
    """Reverify the candidate graph and derive its exact affected-resource set."""
    try:
        prepared.validate()
        verify_candidate_risk_classification(risk, dependencies, impact, runtime)
        evidence = prepared.evidence
        if (
            risk.target != evidence.target
            or risk.repository_id != evidence.repository_id
            or risk.baseline_sha != evidence.baseline_sha
            or risk.candidate_sha != evidence.candidate_sha
            or risk.stage_manifest_sha256 != evidence.stage_manifest_sha256
            or risk.runtime_sha256 != evidence.runtime_sha256
            or risk.level != evidence.risk_level
        ):
            _invalid_state()
        return ResourceAvailabilityTarget.create(prepared, risk.affected_entities)
    except ResourceAvailabilityError:
        raise
    except (CandidateRiskError, PreparedDeploymentError, AttributeError):
        _invalid_state()


@dataclass(frozen=True, slots=True, init=False)
class ResourceAvailabilityObservation:
    deployment_id: str
    startup_error_sha256: str
    target_sha256: str
    observed_at: datetime
    expected_count: int
    available_count: int
    record_sha256: str

    @classmethod
    def create(
        cls,
        deployment_id: str,
        startup_error_sha256: str,
        target_sha256: str,
        observed_at: datetime,
        expected_count: int,
        available_count: int,
    ) -> ResourceAvailabilityObservation:
        when = _timestamp(observed_at)
        values: tuple[object, ...] = (
            deployment_id,
            startup_error_sha256,
            target_sha256,
            when.isoformat(),
            expected_count,
            available_count,
        )
        result = object.__new__(cls)
        for name, value in zip(
            (
                "deployment_id",
                "startup_error_sha256",
                "target_sha256",
                "observed_at",
                "expected_count",
                "available_count",
                "record_sha256",
            ),
            (*values[:3], when, *values[4:], _digest(values)),
            strict=True,
        ):
            object.__setattr__(result, name, value)
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> ResourceAvailabilityObservation:
        if len(row) != 7:
            _invalid_state()
        try:
            when = datetime.fromisoformat(_text(row[3]))
        except ValueError:
            _invalid_state()
        result = object.__new__(cls)
        for name, value in zip(
            (
                "deployment_id",
                "startup_error_sha256",
                "target_sha256",
                "observed_at",
                "expected_count",
                "available_count",
                "record_sha256",
            ),
            (_text(row[0]), _text(row[1]), _text(row[2]), when, row[4], row[5], _text(row[6])),
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
            self.startup_error_sha256,
            self.target_sha256,
            self.observed_at.astimezone(UTC).isoformat(),
            self.expected_count,
            self.available_count,
        )
        return (*values, _digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        if (
            _HASH.fullmatch(self.startup_error_sha256) is None
            or _HASH.fullmatch(self.target_sha256) is None
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
            or type(self.expected_count) is not int
            or type(self.available_count) is not int
            or not 0 <= self.available_count <= self.expected_count
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class ResourceAvailabilityResult:
    status: str
    replayed: bool
    expected_count: int
    available_count: int


def observe_changed_resources_once(
    store: StateStore,
    target: ResourceAvailabilityTarget,
    *,
    token: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
    session_factory: SessionFactory | None = None,
    observed_at: datetime | None = None,
) -> ResourceAvailabilityResult:
    target._validate()
    startup = _prerequisite(store, target)
    existing = load_resource_availability_observation(store, target)
    if existing is not None:
        return _result(existing, True)
    if target.entity_ids:
        states = _probe_states(token, timeout_seconds, max_message_bytes, session_factory)
        available = _require_resources(states, target.entity_ids)
    else:
        available = 0
    requested = ResourceAvailabilityObservation.create(
        target.deployment_id,
        startup.record_sha256,
        target.target_sha256,
        _timestamp(observed_at),
        len(target.entity_ids),
        available,
    )
    if requested.observed_at < startup.observed_at:
        _invalid_state()
    return _record(store, target, requested)


def load_resource_availability_observation(
    store: StateStore, target: ResourceAvailabilityTarget
) -> ResourceAvailabilityObservation | None:
    try:
        target._validate()
        rows = store._connection.execute(
            "SELECT deployment_id, startup_error_sha256, target_sha256, observed_at, "
            "expected_count, available_count, record_sha256 "
            "FROM resource_availability_observation WHERE deployment_id = ?",
            (target.deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = ResourceAvailabilityObservation.from_database_row(tuple(rows[0]))
        startup = _prerequisite(store, target)
        if (
            result.startup_error_sha256 != startup.record_sha256
            or result.target_sha256 != target.target_sha256
            or result.expected_count != len(target.entity_ids)
            or result.available_count != result.expected_count
            or result.observed_at < startup.observed_at
        ):
            _invalid_state()
        return result
    except ResourceAvailabilityError:
        raise
    except (StartupErrorObservationError, StateError, sqlite3.Error, AttributeError):
        _invalid_state()


def _record(
    store: StateStore,
    target: ResourceAvailabilityTarget,
    requested: ResourceAvailabilityObservation,
) -> ResourceAvailabilityResult:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            startup = _prerequisite(store, target)
            if (
                requested.startup_error_sha256 != startup.record_sha256
                or requested.observed_at < startup.observed_at
            ):
                _invalid_state()
            existing = load_resource_availability_observation(store, target)
            if existing is not None:
                return _result(existing, True)
            db.execute(
                "INSERT INTO resource_availability_observation VALUES (?, ?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_resource_availability_observation(store, target)
        if loaded != requested:
            _invalid_state()
        return _result(requested, False)
    except ResourceAvailabilityError:
        raise
    except (StartupErrorObservationError, StateError, sqlite3.Error):
        _invalid_state()


def _prerequisite(store: StateStore, target: ResourceAvailabilityTarget) -> StartupErrorObservation:
    prepared = store.prepared_deployment(target.deployment_id)
    if prepared is None:
        _invalid_state()
    prepared.validate()
    if prepared.evidence.candidate_sha != target.candidate_sha:
        _invalid_state()
    startup = load_startup_error_observation(store, target.deployment_id)
    if startup is None or startup.significant_error_count != 0:
        raise ResourceAvailabilityError("A clear startup-error observation is required")
    return startup


def _probe_states(
    token: str | None, timeout: float, limit: int, factory: SessionFactory | None
) -> list[object]:
    try:
        bearer = _resolve_token(token)
        _limits(timeout, limit)
        with (factory or _default_factory)(_CORE_WEBSOCKET_URL, timeout, limit) as session:
            if _receive(session, timeout, limit).get("type") != "auth_required":
                _unavailable()
            _send(session, {"type": "auth", "access_token": bearer}, limit)
            if _receive(session, timeout, limit).get("type") != "auth_ok":
                _unavailable()
            _send(session, {"id": 1, "type": "get_states"}, limit)
            response = _receive(session, timeout, limit)
            if (
                set(response) != {"id", "type", "success", "result"}
                or response.get("id") != 1
                or response.get("type") != "result"
                or response.get("success") is not True
                or type(response.get("result")) is not list
            ):
                _unavailable()
            result = response.get("result")
            if type(result) is not list:
                _unavailable()
            return result
    except ResourceAvailabilityError:
        raise
    except Exception:
        _unavailable()


def _require_resources(states: list[object], expected: tuple[str, ...]) -> int:
    found: set[str] = set()
    for item in states:
        if (
            type(item) is not dict
            or "entity_id" not in item
            or "state" not in item
            or "attributes" not in item
        ):
            _unavailable()
        entity = item.get("entity_id")
        if type(entity) is not str or _ENTITY_ID.fullmatch(entity) is None or entity in found:
            _unavailable()
        found.add(entity)
    if not set(expected).issubset(found):
        _unavailable()
    return len(expected)


def _receive(session: WebSocketSession, timeout: float, limit: int) -> dict[str, object]:
    message = session.recv(timeout=timeout)
    if type(message) is bytes:
        if len(message) > limit:
            _unavailable()
        message = message.decode("utf-8")
    if type(message) is not str or len(message.encode()) > limit:
        _unavailable()
    value = json.loads(message, object_pairs_hook=_unique, parse_constant=lambda _: _unavailable())
    if type(value) is not dict:
        _unavailable()
    return value


def _send(session: WebSocketSession, payload: dict[str, object], limit: int) -> None:
    message = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(message.encode()) > limit:
        _unavailable()
    session.send(message)


def _default_factory(
    url: str, timeout: float, limit: int
) -> AbstractContextManager[WebSocketSession]:
    return connect(url, open_timeout=timeout, close_timeout=timeout, max_size=limit)


def _resolve_token(token: str | None) -> str:
    value = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        _unavailable()
    return value


def _limits(timeout: float, limit: int) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not 0 < timeout <= 60
        or type(limit) is not int
        or not 0 < limit <= 16 * 1024 * 1024
    ):
        _unavailable()


def _canonical_entities(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple or any(
        type(v) is not str or _ENTITY_ID.fullmatch(v) is None for v in values
    ):
        _invalid_state()
    canonical = tuple(sorted(set(values)))
    if canonical != values:
        _invalid_state()
    return canonical


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


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


def _result(value: ResourceAvailabilityObservation, replayed: bool) -> ResourceAvailabilityResult:
    return ResourceAvailabilityResult(
        "available", replayed, value.expected_count, value.available_count
    )


def _invalid_state() -> NoReturn:
    raise ResourceAvailabilityError("resource availability state is invalid") from None


def _unavailable() -> NoReturn:
    raise ResourceAvailabilityError("changed resource availability is unavailable") from None
