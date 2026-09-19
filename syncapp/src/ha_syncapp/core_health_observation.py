"""Durable, one-shot proof that Core became reachable after restart."""

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

from .core_restart_transport import (
    CoreRestartAttempt,
    CoreRestartError,
    load_core_restart_attempt,
)
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .state import StateError, StateStore

_CORE_API_URL: Final = "http://supervisor/core/api/"
_CORE_API_PATH: Final = "/core/api/"
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_RESPONSE_BYTES: Final = 16 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")


class CoreHealthError(RuntimeError):
    """The post-restart Core health proof could not be established safely."""


@dataclass(frozen=True, slots=True)
class CoreHealthResponse:
    """Bounded Core API response used by the health transport and tests."""

    status: int
    content_type: str
    body: bytes


CoreHealthTransport = Callable[[str, str, Mapping[str, str], float, int], CoreHealthResponse]


@dataclass(frozen=True, slots=True, init=False)
class CoreHealthObservation:
    """Content-free durable evidence bound to one acknowledged restart."""

    deployment_id: str
    restart_attempt_sha256: str
    observed_at: datetime
    record_sha256: str

    @classmethod
    def create(
        cls, deployment_id: str, restart_attempt_sha256: str, observed_at: datetime
    ) -> CoreHealthObservation:
        when = _timestamp(observed_at)
        values: tuple[object, ...] = (
            deployment_id,
            restart_attempt_sha256,
            when.isoformat(),
        )
        result = _construct_observation(
            (deployment_id, restart_attempt_sha256, when, _record_digest(values))
        )
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CoreHealthObservation:
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
            self.restart_attempt_sha256,
            self.observed_at.astimezone(UTC).isoformat(),
        )
        return (*values, _record_digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        if (
            _HASH.fullmatch(self.restart_attempt_sha256) is None
            or not _aware(self.observed_at)
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class CoreHealthResult:
    """Sanitized outcome of one bounded Core health proof."""

    status: str
    replayed: bool


def observe_core_api_once(
    store: StateStore,
    deployment_id: str,
    *,
    token: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    transport: CoreHealthTransport | None = None,
    observed_at: datetime | None = None,
) -> CoreHealthResult:
    """Probe Core once after an acknowledged restart and persist exact evidence."""
    restart = _acknowledged_restart(store, deployment_id)
    existing = load_core_health_observation(store, deployment_id)
    if existing is not None:
        return CoreHealthResult("healthy", replayed=True)

    bearer = _resolve_token(token)
    _validate_limits(timeout_seconds, max_response_bytes)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {bearer}"}
    sender = transport or _default_transport
    try:
        response = sender("GET", _CORE_API_URL, headers, timeout_seconds, max_response_bytes)
        _validate_response(response, max_response_bytes)
    except Exception:
        raise CoreHealthError("Core API health is unavailable") from None

    requested = CoreHealthObservation.create(
        deployment_id, restart.record_sha256, _timestamp(observed_at)
    )
    return _record_observation(store, requested)


def load_core_health_observation(
    store: StateStore, deployment_id: str
) -> CoreHealthObservation | None:
    """Load a health proof while rechecking its exact restart binding."""
    if type(store) is not StateStore:
        _invalid_state()
    try:
        validate_deployment_id(deployment_id)
        rows = store._connection.execute(
            "SELECT deployment_id, restart_attempt_sha256, observed_at, record_sha256 "
            "FROM core_health_observation WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = CoreHealthObservation.from_database_row(tuple(rows[0]))
        restart = load_core_restart_attempt(store, deployment_id)
        if (
            restart is None
            or restart.phase != "request_acknowledged"
            or restart.record_sha256 != result.restart_attempt_sha256
        ):
            _invalid_state()
        return result
    except CoreHealthError:
        raise
    except (PreparedDeploymentError, CoreRestartError, StateError, sqlite3.Error):
        _invalid_state()


def _record_observation(store: StateStore, requested: CoreHealthObservation) -> CoreHealthResult:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            restart = _acknowledged_restart(store, requested.deployment_id)
            if restart.record_sha256 != requested.restart_attempt_sha256:
                _invalid_state()
            existing = load_core_health_observation(store, requested.deployment_id)
            if existing is not None:
                return CoreHealthResult("healthy", replayed=True)
            db.execute(
                "INSERT INTO core_health_observation "
                "(deployment_id, restart_attempt_sha256, observed_at, record_sha256) "
                "VALUES (?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_core_health_observation(store, requested.deployment_id)
        if loaded != requested:
            _invalid_state()
        return CoreHealthResult("healthy", replayed=False)
    except CoreHealthError:
        raise
    except (CoreRestartError, StateError, sqlite3.Error):
        _invalid_state()


def _acknowledged_restart(store: StateStore, deployment_id: str) -> CoreRestartAttempt:
    if type(store) is not StateStore:
        _invalid_state()
    try:
        validate_deployment_id(deployment_id)
        restart = load_core_restart_attempt(store, deployment_id)
    except (PreparedDeploymentError, CoreRestartError, StateError, sqlite3.Error):
        _invalid_state()
    if restart is None or restart.phase != "request_acknowledged":
        raise CoreHealthError("Core restart is not acknowledged")
    return restart


def _validate_response(response: CoreHealthResponse, max_response_bytes: int) -> None:
    if (
        type(response) is not CoreHealthResponse
        or response.status != 200
        or _media_type(response.content_type) != "application/json"
        or type(response.body) is not bytes
        or len(response.body) > max_response_bytes
    ):
        raise CoreHealthError("Core API health response is invalid")
    try:
        payload = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CoreHealthError("Core API health response is invalid") from None
    if payload != {"message": "API running."}:
        raise CoreHealthError("Core API health response is invalid")


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> CoreHealthResponse:
    if method != "GET" or url != _CORE_API_URL:
        raise CoreHealthError("Core API health request boundary is invalid")
    connection = http.client.HTTPConnection("supervisor", 80, timeout=timeout_seconds)
    try:
        connection.request("GET", _CORE_API_PATH, headers=dict(headers))
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "") or ""
        content_length = response.getheader("Content-Length")
        if content_length is not None and int(content_length) > max_response_bytes:
            raise CoreHealthError("Core API health response exceeds size limit")
        body = response.read(max_response_bytes + 1)
        status = int(response.status)
    except CoreHealthError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        raise CoreHealthError("Core API health request failed") from None
    finally:
        connection.close()
    return CoreHealthResponse(status, content_type, body)


def _resolve_token(token: str | None) -> str:
    candidate = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if (
        not isinstance(candidate, str)
        or not candidate.strip()
        or candidate != candidate.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate)
    ):
        raise CoreHealthError("Supervisor API credential is invalid")
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
        raise CoreHealthError("Core API health transport limits are invalid")


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


def _construct_observation(values: tuple[object, ...]) -> CoreHealthObservation:
    if len(values) != 4:
        _invalid_state()
    result = object.__new__(CoreHealthObservation)
    for name, value in zip(CoreHealthObservation.__slots__, values, strict=True):
        object.__setattr__(result, name, value)
    return result


def _timestamp(value: datetime | None = None) -> datetime:
    when = datetime.now(UTC) if value is None else value
    if not _aware(when):
        raise CoreHealthError("Core API health timestamp is invalid")
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
    raise CoreHealthError("Core health observation state is invalid") from None
