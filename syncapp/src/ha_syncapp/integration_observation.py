"""Durable proof that every enabled Home Assistant integration initialized."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, NoReturn, Protocol

from websockets.sync.client import connect

from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .state import StateError, StateStore
from .supervisor_health_observation import (
    SupervisorHealthError,
    SupervisorHealthObservation,
    load_supervisor_health_observation,
)

_CORE_WEBSOCKET_URL: Final = "ws://supervisor/core/websocket"
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_MESSAGE_BYTES: Final = 4 * 1024 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")


class IntegrationObservationError(RuntimeError):
    """Integration initialization could not be proved safely."""


class WebSocketSession(Protocol):
    """Minimal synchronous session boundary used by production and tests."""

    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...


SessionFactory = Callable[[str, float, int], AbstractContextManager[WebSocketSession]]


@dataclass(frozen=True, slots=True, init=False)
class IntegrationObservation:
    """Content-free aggregate bound to one exact Supervisor health proof."""

    deployment_id: str
    supervisor_health_sha256: str
    observed_at: datetime
    entry_count: int
    disabled_count: int
    record_sha256: str

    @classmethod
    def create(
        cls,
        deployment_id: str,
        supervisor_health_sha256: str,
        observed_at: datetime,
        entry_count: int,
        disabled_count: int,
    ) -> IntegrationObservation:
        when = _timestamp(observed_at)
        values: tuple[object, ...] = (
            deployment_id,
            supervisor_health_sha256,
            when.isoformat(),
            entry_count,
            disabled_count,
        )
        result = _construct(
            (
                deployment_id,
                supervisor_health_sha256,
                when,
                entry_count,
                disabled_count,
                _record_digest(values),
            )
        )
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> IntegrationObservation:
        if len(row) != 6:
            _invalid_state()
        try:
            observed_at = datetime.fromisoformat(_text(row[2]))
        except ValueError:
            _invalid_state()
        result = _construct(
            (_text(row[0]), _text(row[1]), observed_at, row[3], row[4], _text(row[5]))
        )
        result._validate()
        if result.database_values() != row:
            _invalid_state()
        return result

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        values: tuple[object, ...] = (
            self.deployment_id,
            self.supervisor_health_sha256,
            self.observed_at.astimezone(UTC).isoformat(),
            self.entry_count,
            self.disabled_count,
        )
        return (*values, _record_digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        if (
            _HASH.fullmatch(self.supervisor_health_sha256) is None
            or not _aware(self.observed_at)
            or type(self.entry_count) is not int
            or type(self.disabled_count) is not int
            or self.entry_count < 0
            or not 0 <= self.disabled_count <= self.entry_count
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class IntegrationObservationResult:
    """Sanitized result of one integration initialization observation."""

    status: str
    replayed: bool
    entry_count: int
    disabled_count: int


def observe_integrations_once(
    store: StateStore,
    deployment_id: str,
    *,
    token: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
    session_factory: SessionFactory | None = None,
    observed_at: datetime | None = None,
) -> IntegrationObservationResult:
    """Observe exactly one config-entry snapshot and persist aggregate proof."""
    supervisor = _supervisor_proof(store, deployment_id)
    existing = load_integration_observation(store, deployment_id)
    if existing is not None:
        return _result(existing, replayed=True)

    entry_count, disabled_count = _probe_integrations(
        token=token,
        timeout_seconds=timeout_seconds,
        max_message_bytes=max_message_bytes,
        session_factory=session_factory,
    )
    requested = IntegrationObservation.create(
        deployment_id,
        supervisor.record_sha256,
        _timestamp(observed_at),
        entry_count,
        disabled_count,
    )
    if requested.observed_at < supervisor.observed_at:
        _invalid_state()
    return _record_observation(store, requested)


def load_integration_observation(
    store: StateStore, deployment_id: str
) -> IntegrationObservation | None:
    """Load aggregate evidence while revalidating its Supervisor binding."""
    if type(store) is not StateStore:
        _invalid_state()
    try:
        validate_deployment_id(deployment_id)
        rows = store._connection.execute(
            "SELECT deployment_id, supervisor_health_sha256, observed_at, "
            "entry_count, disabled_count, record_sha256 "
            "FROM integration_observation WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = IntegrationObservation.from_database_row(tuple(rows[0]))
        supervisor = _supervisor_proof(store, deployment_id)
        if (
            result.supervisor_health_sha256 != supervisor.record_sha256
            or result.observed_at < supervisor.observed_at
        ):
            _invalid_state()
        return result
    except IntegrationObservationError:
        raise
    except (PreparedDeploymentError, SupervisorHealthError, StateError, sqlite3.Error):
        _invalid_state()


def _record_observation(
    store: StateStore, requested: IntegrationObservation
) -> IntegrationObservationResult:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            supervisor = _supervisor_proof(store, requested.deployment_id)
            if (
                requested.supervisor_health_sha256 != supervisor.record_sha256
                or requested.observed_at < supervisor.observed_at
            ):
                _invalid_state()
            existing = load_integration_observation(store, requested.deployment_id)
            if existing is not None:
                return _result(existing, replayed=True)
            db.execute(
                "INSERT INTO integration_observation "
                "(deployment_id, supervisor_health_sha256, observed_at, entry_count, "
                "disabled_count, record_sha256) VALUES (?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_integration_observation(store, requested.deployment_id)
        if loaded != requested:
            _invalid_state()
        return _result(requested, replayed=False)
    except IntegrationObservationError:
        raise
    except (SupervisorHealthError, StateError, sqlite3.Error):
        _invalid_state()


def _probe_integrations(
    *,
    token: str | None,
    timeout_seconds: float,
    max_message_bytes: int,
    session_factory: SessionFactory | None,
) -> tuple[int, int]:
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
            _send_json(session, {"id": 1, "type": "config_entries/get"}, max_message_bytes)
            response = _receive_json(session, timeout_seconds, max_message_bytes)
            if (
                type(response) is not dict
                or set(response) != {"id", "type", "success", "result"}
                or response.get("id") != 1
                or response.get("type") != "result"
                or response.get("success") is not True
            ):
                _unavailable()
            return _validate_entries(response.get("result"))
    except IntegrationObservationError:
        raise
    except Exception:
        _unavailable()


def _validate_entries(value: object) -> tuple[int, int]:
    if type(value) is not list:
        _unavailable()
    identities: set[str] = set()
    disabled_count = 0
    for entry in value:
        if type(entry) is not dict or any(type(key) is not str for key in entry):
            _unavailable()
        if not {"entry_id", "state", "disabled_by"} <= set(entry):
            _unavailable()
        identity = entry.get("entry_id")
        state = entry.get("state")
        disabled_by = entry.get("disabled_by")
        if type(identity) is not str or not identity or identity in identities:
            _unavailable()
        identities.add(identity)
        if disabled_by is None:
            if state != "loaded" or type(state) is not str:
                _unavailable()
        elif type(disabled_by) is str and disabled_by:
            disabled_count += 1
        else:
            _unavailable()
    return len(value), disabled_count


def _supervisor_proof(store: StateStore, deployment_id: str) -> SupervisorHealthObservation:
    if type(store) is not StateStore:
        _invalid_state()
    try:
        result = load_supervisor_health_observation(store, deployment_id)
    except (SupervisorHealthError, StateError, sqlite3.Error):
        _invalid_state()
    if result is None:
        raise IntegrationObservationError("Supervisor health proof is required")
    return result


def _result(observation: IntegrationObservation, *, replayed: bool) -> IntegrationObservationResult:
    return IntegrationObservationResult(
        "initialized", replayed, observation.entry_count, observation.disabled_count
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
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        _unavailable()


def _send_json(
    session: WebSocketSession, payload: Mapping[str, object], max_message_bytes: int
) -> None:
    try:
        message = json.dumps(dict(payload), allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        _unavailable()
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


def _construct(values: tuple[object, ...]) -> IntegrationObservation:
    if len(values) != 6:
        _invalid_state()
    result = object.__new__(IntegrationObservation)
    for name, value in zip(IntegrationObservation.__slots__, values, strict=True):
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
    raise IntegrationObservationError("Integration initialization is unavailable") from None


def _invalid_state() -> NoReturn:
    raise IntegrationObservationError("Integration observation state is invalid") from None
