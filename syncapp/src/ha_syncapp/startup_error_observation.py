"""Durable post-restart proof of Home Assistant startup log severity."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, NoReturn

from websockets.sync.client import connect

from .core_restart_transport import (
    CoreRestartAttempt,
    CoreRestartError,
    load_core_restart_attempt,
)
from .integration_observation import (
    IntegrationObservation,
    IntegrationObservationError,
    SessionFactory,
    WebSocketSession,
    load_integration_observation,
)
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .state import StateError, StateStore

_CORE_WEBSOCKET_URL: Final = "ws://supervisor/core/websocket"
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_MESSAGE_BYTES: Final = 4 * 1024 * 1024
_ENTRY_KEYS: Final = {
    "name",
    "message",
    "level",
    "source",
    "timestamp",
    "exception",
    "count",
    "first_occurred",
}
_LEVELS: Final = {"WARNING", "ERROR", "CRITICAL"}
_HASH = re.compile(r"^[0-9a-f]{64}$")


class StartupErrorObservationError(RuntimeError):
    """Startup error evidence could not be observed or validated safely."""


@dataclass(frozen=True, slots=True, init=False)
class StartupErrorObservation:
    """Content-free severity aggregate bound to the exact deployment chain."""

    deployment_id: str
    integration_observation_sha256: str
    restart_attempt_sha256: str
    interval_started_at: datetime
    observed_at: datetime
    inspected_count: int
    warning_count: int
    significant_error_count: int
    record_sha256: str

    @classmethod
    def create(
        cls,
        deployment_id: str,
        integration_observation_sha256: str,
        restart_attempt_sha256: str,
        interval_started_at: datetime,
        observed_at: datetime,
        inspected_count: int,
        warning_count: int,
        significant_error_count: int,
    ) -> StartupErrorObservation:
        started = _timestamp(interval_started_at)
        observed = _timestamp(observed_at)
        values: tuple[object, ...] = (
            deployment_id,
            integration_observation_sha256,
            restart_attempt_sha256,
            started.isoformat(),
            observed.isoformat(),
            inspected_count,
            warning_count,
            significant_error_count,
        )
        result = _construct(
            (
                deployment_id,
                integration_observation_sha256,
                restart_attempt_sha256,
                started,
                observed,
                inspected_count,
                warning_count,
                significant_error_count,
                _record_digest(values),
            )
        )
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> StartupErrorObservation:
        if len(row) != 9:
            _invalid_state()
        try:
            started = datetime.fromisoformat(_text(row[3]))
            observed = datetime.fromisoformat(_text(row[4]))
        except ValueError:
            _invalid_state()
        result = _construct(
            (
                _text(row[0]),
                _text(row[1]),
                _text(row[2]),
                started,
                observed,
                row[5],
                row[6],
                row[7],
                _text(row[8]),
            )
        )
        result._validate()
        if result.database_values() != row:
            _invalid_state()
        return result

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        values: tuple[object, ...] = (
            self.deployment_id,
            self.integration_observation_sha256,
            self.restart_attempt_sha256,
            self.interval_started_at.astimezone(UTC).isoformat(),
            self.observed_at.astimezone(UTC).isoformat(),
            self.inspected_count,
            self.warning_count,
            self.significant_error_count,
        )
        return (*values, _record_digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        counts = (self.inspected_count, self.warning_count, self.significant_error_count)
        if (
            _HASH.fullmatch(self.integration_observation_sha256) is None
            or _HASH.fullmatch(self.restart_attempt_sha256) is None
            or not _aware(self.interval_started_at)
            or not _aware(self.observed_at)
            or self.observed_at < self.interval_started_at
            or any(type(value) is not int or value < 0 for value in counts)
            or self.warning_count + self.significant_error_count > self.inspected_count
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class StartupErrorObservationResult:
    """Sanitized durable outcome of one startup-error observation."""

    status: str
    replayed: bool
    inspected_count: int
    warning_count: int
    significant_error_count: int


def observe_startup_errors_once(
    store: StateStore,
    deployment_id: str,
    *,
    token: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
    session_factory: SessionFactory | None = None,
    observed_at: datetime | None = None,
) -> StartupErrorObservationResult:
    """Observe system-log severity once and durably remember either outcome."""
    integration, restart = _prerequisites(store, deployment_id)
    existing = load_startup_error_observation(store, deployment_id)
    if existing is not None:
        return _result(existing, replayed=True)

    entries = _probe_system_log(
        token=token,
        timeout_seconds=timeout_seconds,
        max_message_bytes=max_message_bytes,
        session_factory=session_factory,
    )
    when = _timestamp(observed_at)
    if when < integration.observed_at:
        _invalid_state()
    warning_count, significant_count = _classify_entries(
        entries, interval_started_at=restart.updated_at, observed_at=when
    )
    requested = StartupErrorObservation.create(
        deployment_id,
        integration.record_sha256,
        restart.record_sha256,
        restart.updated_at,
        when,
        len(entries),
        warning_count,
        significant_count,
    )
    return _record_observation(store, requested)


def load_startup_error_observation(
    store: StateStore, deployment_id: str
) -> StartupErrorObservation | None:
    """Load evidence while revalidating the exact restart and integration chain."""
    if type(store) is not StateStore:
        _invalid_state()
    try:
        validate_deployment_id(deployment_id)
        rows = store._connection.execute(
            "SELECT deployment_id, integration_observation_sha256, "
            "restart_attempt_sha256, interval_started_at, observed_at, inspected_count, "
            "warning_count, significant_error_count, record_sha256 "
            "FROM startup_error_observation WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = StartupErrorObservation.from_database_row(tuple(rows[0]))
        integration, restart = _prerequisites(store, deployment_id)
        if (
            result.integration_observation_sha256 != integration.record_sha256
            or result.restart_attempt_sha256 != restart.record_sha256
            or result.interval_started_at != restart.updated_at
            or result.observed_at < integration.observed_at
        ):
            _invalid_state()
        return result
    except StartupErrorObservationError:
        raise
    except (
        PreparedDeploymentError,
        IntegrationObservationError,
        CoreRestartError,
        StateError,
        sqlite3.Error,
    ):
        _invalid_state()


def _record_observation(
    store: StateStore, requested: StartupErrorObservation
) -> StartupErrorObservationResult:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            integration, restart = _prerequisites(store, requested.deployment_id)
            if (
                requested.integration_observation_sha256 != integration.record_sha256
                or requested.restart_attempt_sha256 != restart.record_sha256
                or requested.interval_started_at != restart.updated_at
                or requested.observed_at < integration.observed_at
            ):
                _invalid_state()
            existing = load_startup_error_observation(store, requested.deployment_id)
            if existing is not None:
                return _result(existing, replayed=True)
            db.execute(
                "INSERT INTO startup_error_observation "
                "(deployment_id, integration_observation_sha256, restart_attempt_sha256, "
                "interval_started_at, observed_at, inspected_count, warning_count, "
                "significant_error_count, record_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_startup_error_observation(store, requested.deployment_id)
        if loaded != requested:
            _invalid_state()
        return _result(requested, replayed=False)
    except StartupErrorObservationError:
        raise
    except (IntegrationObservationError, CoreRestartError, StateError, sqlite3.Error):
        _invalid_state()


def _prerequisites(
    store: StateStore, deployment_id: str
) -> tuple[IntegrationObservation, CoreRestartAttempt]:
    if type(store) is not StateStore:
        _invalid_state()
    try:
        integration = load_integration_observation(store, deployment_id)
        restart = load_core_restart_attempt(store, deployment_id)
    except (IntegrationObservationError, CoreRestartError, StateError, sqlite3.Error):
        _invalid_state()
    if integration is None:
        raise StartupErrorObservationError("Integration initialization proof is required")
    if restart is None or restart.phase != "request_acknowledged":
        _invalid_state()
    return integration, restart


def _probe_system_log(
    *,
    token: str | None,
    timeout_seconds: float,
    max_message_bytes: int,
    session_factory: SessionFactory | None,
) -> list[dict[str, object]]:
    try:
        bearer = _resolve_token(token)
        _validate_limits(timeout_seconds, max_message_bytes)
        factory = session_factory or _default_session_factory
        with factory(_CORE_WEBSOCKET_URL, timeout_seconds, max_message_bytes) as session:
            required = _receive_json(session, timeout_seconds, max_message_bytes)
            if type(required) is not dict or required.get("type") != "auth_required":
                _unavailable()
            _send_json(session, {"type": "auth", "access_token": bearer}, max_message_bytes)
            authenticated = _receive_json(session, timeout_seconds, max_message_bytes)
            if type(authenticated) is not dict or authenticated.get("type") != "auth_ok":
                _unavailable()
            _send_json(session, {"id": 1, "type": "system_log/list"}, max_message_bytes)
            response = _receive_json(session, timeout_seconds, max_message_bytes)
            if (
                type(response) is not dict
                or set(response) != {"id", "type", "success", "result"}
                or response.get("id") != 1
                or response.get("type") != "result"
                or response.get("success") is not True
                or type(response.get("result")) is not list
            ):
                _unavailable()
            return _validate_entries(response["result"])
    except StartupErrorObservationError:
        raise
    except Exception:
        _unavailable()


def _validate_entries(value: list[object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    fingerprints: set[str] = set()
    for entry in value:
        if type(entry) is not dict or set(entry) != _ENTRY_KEYS:
            _unavailable()
        name = entry.get("name")
        messages = entry.get("message")
        source = entry.get("source")
        exception = entry.get("exception")
        count = entry.get("count")
        level = entry.get("level")
        if (
            type(name) is not str
            or not name
            or type(messages) is not list
            or not 1 <= len(messages) <= 5
            or any(type(message) is not str for message in messages)
            or type(source) is not list
            or len(source) != 2
            or type(source[0]) is not str
            or not source[0]
            or type(source[1]) is not int
            or source[1] < 0
            or type(exception) is not str
            or type(count) is not int
            or count <= 0
            or type(level) is not str
            or level not in _LEVELS
        ):
            _unavailable()
        _epoch(entry.get("timestamp"))
        first = _epoch(entry.get("first_occurred"))
        latest = _epoch(entry.get("timestamp"))
        if first > latest:
            _unavailable()
        fingerprint = json.dumps(entry, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if fingerprint in fingerprints:
            _unavailable()
        fingerprints.add(fingerprint)
        result.append(dict(entry))
    return result


def _classify_entries(
    entries: list[dict[str, object]], *, interval_started_at: datetime, observed_at: datetime
) -> tuple[int, int]:
    warnings = 0
    significant = 0
    for entry in entries:
        occurred_at = _epoch(entry["timestamp"])
        if occurred_at > observed_at:
            _unavailable()
        if occurred_at < interval_started_at:
            continue
        if entry["level"] == "WARNING":
            warnings += 1
        else:
            significant += 1
    return warnings, significant


def _result(
    observation: StartupErrorObservation, *, replayed: bool
) -> StartupErrorObservationResult:
    status = "clear" if observation.significant_error_count == 0 else "significant_errors"
    return StartupErrorObservationResult(
        status,
        replayed,
        observation.inspected_count,
        observation.warning_count,
        observation.significant_error_count,
    )


def _receive_json(
    session: WebSocketSession, timeout_seconds: float, max_message_bytes: int
) -> object:
    message = session.recv(timeout=timeout_seconds)
    if type(message) is bytes:
        if len(message) > max_message_bytes:
            _unavailable()
        try:
            text = message.decode("utf-8")
        except UnicodeDecodeError:
            _unavailable()
    elif type(message) is str:
        if len(message.encode("utf-8")) > max_message_bytes:
            _unavailable()
        text = message
    else:
        _unavailable()
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError):
        _unavailable()


def _send_json(
    session: WebSocketSession, payload: Mapping[str, object], max_message_bytes: int
) -> None:
    message = json.dumps(dict(payload), allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(message.encode("utf-8")) > max_message_bytes:
        _unavailable()
    session.send(message)


def _default_session_factory(
    url: str, timeout_seconds: float, max_message_bytes: int
) -> AbstractContextManager[WebSocketSession]:
    if url != _CORE_WEBSOCKET_URL:
        _unavailable()
    return connect(
        url,
        open_timeout=timeout_seconds,
        close_timeout=timeout_seconds,
        max_size=max_message_bytes,
    )


def _resolve_token(token: str | None) -> str:
    candidate = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if (
        type(candidate) is not str
        or not candidate
        or candidate != candidate.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate)
    ):
        _unavailable()
    return candidate


def _validate_limits(timeout_seconds: float, max_message_bytes: int) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or timeout_seconds <= 0
        or timeout_seconds > 60
        or type(max_message_bytes) is not int
        or not 0 < max_message_bytes <= 16 * 1024 * 1024
    ):
        _unavailable()


def _epoch(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _unavailable()
    number = float(value)
    if not math.isfinite(number):
        _unavailable()
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OSError, OverflowError, ValueError):
        _unavailable()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _record_digest(values: tuple[object, ...]) -> str:
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _construct(values: tuple[object, ...]) -> StartupErrorObservation:
    if len(values) != 9:
        _invalid_state()
    result = object.__new__(StartupErrorObservation)
    for name, value in zip(StartupErrorObservation.__slots__, values, strict=True):
        object.__setattr__(result, name, value)
    return result


def _timestamp(value: datetime | None = None) -> datetime:
    when = datetime.now(UTC) if value is None else value
    if not _aware(when):
        _invalid_state()
    return when.astimezone(UTC)


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _text(value: object) -> str:
    if type(value) is not str:
        _invalid_state()
    return value


def _unavailable() -> NoReturn:
    raise StartupErrorObservationError("Startup error observation is unavailable") from None


def _invalid_state() -> NoReturn:
    raise StartupErrorObservationError("Startup error observation state is invalid") from None
