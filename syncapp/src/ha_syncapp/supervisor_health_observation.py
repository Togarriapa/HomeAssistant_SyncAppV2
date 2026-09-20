"""Durable proof that Supervisor is healthy after the Core observation window."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, NoReturn

from .core_health_window import (
    CoreHealthWindow,
    CoreHealthWindowError,
    load_core_health_window,
)
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .state import StateError, StateStore

_SUPERVISOR_INFO_URL: Final = "http://supervisor/supervisor/info"
_SUPERVISOR_INFO_PATH: Final = "/supervisor/info"
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_RESPONSE_BYTES: Final = 16 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")


class SupervisorHealthError(RuntimeError):
    """The exact Supervisor health proof could not be established safely."""


@dataclass(frozen=True, slots=True)
class SupervisorHealthResponse:
    """Bounded Supervisor response used by the transport and tests."""

    status: int
    content_type: str
    body: bytes


SupervisorHealthTransport = Callable[
    [str, str, Mapping[str, str], float, int], SupervisorHealthResponse
]


@dataclass(frozen=True, slots=True, init=False)
class SupervisorHealthObservation:
    """Content-free evidence bound to one completed Core health window."""

    deployment_id: str
    core_window_sha256: str
    observed_at: datetime
    record_sha256: str

    @classmethod
    def create(
        cls, deployment_id: str, core_window_sha256: str, observed_at: datetime
    ) -> SupervisorHealthObservation:
        when = _timestamp(observed_at)
        values: tuple[object, ...] = (
            deployment_id,
            core_window_sha256,
            when.isoformat(),
        )
        result = _construct_observation(
            (deployment_id, core_window_sha256, when, _record_digest(values))
        )
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> SupervisorHealthObservation:
        if len(row) != 4:
            _invalid_state()
        try:
            observed_at = datetime.fromisoformat(_text(row[2]))
        except ValueError:
            _invalid_state()
        result = _construct_observation((_text(row[0]), _text(row[1]), observed_at, _text(row[3])))
        result._validate()
        if result.database_values() != row:
            _invalid_state()
        return result

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        values: tuple[object, ...] = (
            self.deployment_id,
            self.core_window_sha256,
            self.observed_at.astimezone(UTC).isoformat(),
        )
        return (*values, _record_digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        if (
            _HASH.fullmatch(self.core_window_sha256) is None
            or not _aware(self.observed_at)
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class SupervisorHealthResult:
    """Sanitized outcome of one bounded Supervisor health proof."""

    status: str
    replayed: bool


def observe_supervisor_health_once(
    store: StateStore,
    deployment_id: str,
    *,
    token: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    transport: SupervisorHealthTransport | None = None,
    observed_at: datetime | None = None,
) -> SupervisorHealthResult:
    """Read Supervisor once after the exact Core window and persist proof."""
    window = _completed_window(store, deployment_id)
    existing = load_supervisor_health_observation(store, deployment_id)
    if existing is not None:
        return SupervisorHealthResult("healthy", replayed=True)

    probe_supervisor_health(
        token=token,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        transport=transport,
    )
    requested = SupervisorHealthObservation.create(
        deployment_id, window.record_sha256, _timestamp(observed_at)
    )
    if requested.observed_at < _completed_at(window):
        _invalid_state()
    return _record_observation(store, requested)


def load_supervisor_health_observation(
    store: StateStore, deployment_id: str
) -> SupervisorHealthObservation | None:
    """Load evidence while revalidating its completed-window binding."""
    if type(store) is not StateStore:
        _invalid_state()
    try:
        validate_deployment_id(deployment_id)
        rows = store._connection.execute(
            "SELECT deployment_id, core_window_sha256, observed_at, record_sha256 "
            "FROM supervisor_health_observation WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = SupervisorHealthObservation.from_database_row(tuple(rows[0]))
        window = _completed_window(store, deployment_id)
        if result.core_window_sha256 != window.record_sha256 or result.observed_at < _completed_at(
            window
        ):
            _invalid_state()
        return result
    except SupervisorHealthError:
        raise
    except (PreparedDeploymentError, CoreHealthWindowError, StateError, sqlite3.Error):
        _invalid_state()


def _record_observation(
    store: StateStore, requested: SupervisorHealthObservation
) -> SupervisorHealthResult:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            window = _completed_window(store, requested.deployment_id)
            if (
                window.record_sha256 != requested.core_window_sha256
                or requested.observed_at < _completed_at(window)
            ):
                _invalid_state()
            existing = load_supervisor_health_observation(store, requested.deployment_id)
            if existing is not None:
                return SupervisorHealthResult("healthy", replayed=True)
            db.execute(
                "INSERT INTO supervisor_health_observation "
                "(deployment_id, core_window_sha256, observed_at, record_sha256) "
                "VALUES (?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_supervisor_health_observation(store, requested.deployment_id)
        if loaded != requested:
            _invalid_state()
        return SupervisorHealthResult("healthy", replayed=False)
    except SupervisorHealthError:
        raise
    except (CoreHealthWindowError, StateError, sqlite3.Error):
        _invalid_state()


def _completed_window(store: StateStore, deployment_id: str) -> CoreHealthWindow:
    if type(store) is not StateStore:
        _invalid_state()
    try:
        validate_deployment_id(deployment_id)
        window = load_core_health_window(store, deployment_id)
    except (PreparedDeploymentError, CoreHealthWindowError, StateError, sqlite3.Error):
        _invalid_state()
    if window is None or window.completed_at is None:
        raise SupervisorHealthError("Core health window is not complete")
    return window


def probe_supervisor_health(
    *,
    token: str | None,
    timeout_seconds: float,
    max_response_bytes: int,
    transport: SupervisorHealthTransport | None,
) -> None:
    """Perform one bounded exact Supervisor health probe without persisting authority."""
    bearer = _resolve_token(token)
    _validate_limits(timeout_seconds, max_response_bytes)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {bearer}"}
    sender = transport or _default_transport
    try:
        response = sender(
            "GET",
            _SUPERVISOR_INFO_URL,
            headers,
            timeout_seconds,
            max_response_bytes,
        )
        _validate_response(response, max_response_bytes)
    except Exception:
        raise SupervisorHealthError("Supervisor health is unavailable") from None


def _validate_response(response: SupervisorHealthResponse, max_response_bytes: int) -> None:
    if (
        type(response) is not SupervisorHealthResponse
        or response.status != 200
        or _media_type(response.content_type) != "application/json"
        or type(response.body) is not bytes
        or len(response.body) > max_response_bytes
    ):
        raise SupervisorHealthError("Supervisor health response is invalid")
    try:
        payload = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SupervisorHealthError("Supervisor health response is invalid") from None
    if type(payload) is not dict or set(payload) != {"result", "data"}:
        raise SupervisorHealthError("Supervisor health response is invalid")
    data = payload.get("data")
    if (
        payload.get("result") != "ok"
        or type(data) is not dict
        or data.get("healthy") is not True
        or data.get("supported") is not True
    ):
        raise SupervisorHealthError("Supervisor health response is invalid")


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> SupervisorHealthResponse:
    if method != "GET" or url != _SUPERVISOR_INFO_URL:
        raise SupervisorHealthError("Supervisor health request boundary is invalid")
    connection = http.client.HTTPConnection("supervisor", 80, timeout=timeout_seconds)
    try:
        connection.request("GET", _SUPERVISOR_INFO_PATH, headers=dict(headers))
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "") or ""
        content_length = response.getheader("Content-Length")
        if content_length is not None and int(content_length) > max_response_bytes:
            raise SupervisorHealthError("Supervisor health response exceeds size limit")
        body = response.read(max_response_bytes + 1)
        status = int(response.status)
    except SupervisorHealthError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        raise SupervisorHealthError("Supervisor health request failed") from None
    finally:
        connection.close()
    return SupervisorHealthResponse(status, content_type, body)


def _resolve_token(token: str | None) -> str:
    candidate = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if (
        not isinstance(candidate, str)
        or not candidate.strip()
        or candidate != candidate.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate)
    ):
        raise SupervisorHealthError("Supervisor API credential is invalid")
    return candidate


def _validate_limits(timeout_seconds: float, max_response_bytes: int) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or timeout_seconds <= 0
        or timeout_seconds > 60
        or type(max_response_bytes) is not int
        or not 0 < max_response_bytes <= 1024 * 1024
    ):
        raise SupervisorHealthError("Supervisor health transport limits are invalid")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _record_digest(values: tuple[object, ...]) -> str:
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _construct_observation(
    values: tuple[object, ...],
) -> SupervisorHealthObservation:
    if len(values) != 4:
        _invalid_state()
    result = object.__new__(SupervisorHealthObservation)
    for name, value in zip(SupervisorHealthObservation.__slots__, values, strict=True):
        object.__setattr__(result, name, value)
    return result


def _completed_at(window: CoreHealthWindow) -> datetime:
    if window.completed_at is None:
        _invalid_state()
    return window.completed_at


def _timestamp(value: datetime | None = None) -> datetime:
    when = datetime.now(UTC) if value is None else value
    if not _aware(when):
        raise SupervisorHealthError("Supervisor health timestamp is invalid")
    return when.astimezone(UTC)


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid_state()
    return value


def _invalid_state() -> NoReturn:
    raise SupervisorHealthError("Supervisor health observation state is invalid") from None
