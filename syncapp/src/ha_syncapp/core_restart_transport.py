"""Crash-safe one-shot Supervisor transport for authorized Core restart."""

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

from .post_apply_activation import (
    PostApplyActivationAuthorization,
    PostApplyActivationError,
    load_post_apply_activation_authorization,
)
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .state import StateError, StateStore

_RESTART_URL: Final = "http://supervisor/core/restart"
_RESTART_PATH: Final = "/core/restart"
_REQUEST_BODY: Final = json.dumps(
    {"force": False, "safe_mode": False}, separators=(",", ":")
).encode("utf-8")
_DEFAULT_TIMEOUT_SECONDS: Final = 60.0
_DEFAULT_MAX_RESPONSE_BYTES: Final = 64 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")


class CoreRestartError(RuntimeError):
    """An authorized Core restart could not be requested safely."""


@dataclass(frozen=True, slots=True)
class CoreRestartResponse:
    """Bounded Supervisor response used by the restart transport and tests."""

    status: int
    content_type: str
    body: bytes


CoreRestartTransport = Callable[
    [str, str, Mapping[str, str], bytes, float, int], CoreRestartResponse
]


@dataclass(frozen=True, slots=True, init=False)
class CoreRestartAttempt:
    """Content-free journal evidence for one authorized restart request."""

    deployment_id: str
    authorization_record_sha256: str
    phase: str
    started_at: datetime
    updated_at: datetime
    record_sha256: str

    @classmethod
    def create(
        cls,
        authorization: PostApplyActivationAuthorization,
        phase: str,
        started_at: datetime,
        updated_at: datetime,
    ) -> CoreRestartAttempt:
        start = _timestamp(started_at)
        update = _timestamp(updated_at)
        values: tuple[object, ...] = (
            authorization.deployment_id,
            authorization.record_sha256,
            phase,
            start.isoformat(),
            update.isoformat(),
        )
        result = _construct_attempt(
            (
                authorization.deployment_id,
                authorization.record_sha256,
                phase,
                start,
                update,
                _record_digest(values),
            )
        )
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CoreRestartAttempt:
        if len(row) != 6:
            _invalid_state()
        try:
            started_at = datetime.fromisoformat(_text(row[3]))
            updated_at = datetime.fromisoformat(_text(row[4]))
        except ValueError:
            _invalid_state()
        result = _construct_attempt(
            (
                _text(row[0]),
                _text(row[1]),
                _text(row[2]),
                started_at,
                updated_at,
                _text(row[5]),
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
            self.authorization_record_sha256,
            self.phase,
            self.started_at.astimezone(UTC).isoformat(),
            self.updated_at.astimezone(UTC).isoformat(),
        )
        return (*values, _record_digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        if (
            _HASH.fullmatch(self.authorization_record_sha256) is None
            or self.phase not in {"request_started", "request_acknowledged"}
            or not _aware(self.started_at)
            or not _aware(self.updated_at)
            or self.updated_at < self.started_at
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class CoreRestartResult:
    """Sanitized result of one bounded restart transport invocation."""

    status: str
    replayed: bool


def request_core_restart_once(
    store: StateStore,
    authorization: PostApplyActivationAuthorization,
    *,
    token: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    transport: CoreRestartTransport | None = None,
    now: datetime | None = None,
) -> CoreRestartResult:
    """Journal first, then request one exact restart without blind replay."""
    _validate_authorization(store, authorization)
    bearer = _resolve_token(token)
    _validate_limits(timeout_seconds, max_response_bytes)
    when = _timestamp(now)
    existing = load_core_restart_attempt(store, authorization.deployment_id)
    if existing is not None:
        if existing.phase == "request_acknowledged":
            return CoreRestartResult("request_acknowledged", replayed=True)
        return CoreRestartResult("reconciliation_required", replayed=True)

    attempt, inserted = _record_started(store, authorization, when)
    if not inserted:
        if attempt.phase == "request_acknowledged":
            return CoreRestartResult("request_acknowledged", replayed=True)
        return CoreRestartResult("reconciliation_required", replayed=True)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }
    sender = transport or _default_transport
    try:
        response = sender(
            "POST",
            _RESTART_URL,
            headers,
            _REQUEST_BODY,
            timeout_seconds,
            max_response_bytes,
        )
        _validate_response(response, max_response_bytes)
    except Exception:
        raise CoreRestartError("Core restart outcome is uncertain") from None
    _record_acknowledged(store, authorization, when=_timestamp())
    return CoreRestartResult("request_acknowledged", replayed=False)


def load_core_restart_attempt(store: StateStore, deployment_id: str) -> CoreRestartAttempt | None:
    """Load one attempt while rechecking its exact durable authorization binding."""
    if type(store) is not StateStore:
        _invalid_state()
    try:
        validate_deployment_id(deployment_id)
        rows = store._connection.execute(
            "SELECT deployment_id, authorization_record_sha256, phase, started_at, "
            "updated_at, record_sha256 FROM core_restart_attempt WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = CoreRestartAttempt.from_database_row(tuple(rows[0]))
        authorization = load_post_apply_activation_authorization(store, deployment_id)
        if (
            authorization is None
            or authorization.record_sha256 != result.authorization_record_sha256
        ):
            _invalid_state()
        return result
    except CoreRestartError:
        raise
    except (PreparedDeploymentError, PostApplyActivationError, StateError, sqlite3.Error):
        _invalid_state()


def _record_started(
    store: StateStore,
    authorization: PostApplyActivationAuthorization,
    when: datetime,
) -> tuple[CoreRestartAttempt, bool]:
    requested = CoreRestartAttempt.create(authorization, "request_started", when, when)
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _validate_authorization(store, authorization)
            existing = load_core_restart_attempt(store, authorization.deployment_id)
            if existing is not None:
                return existing, False
            db.execute(
                "INSERT INTO core_restart_attempt (deployment_id, "
                "authorization_record_sha256, phase, started_at, updated_at, record_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_core_restart_attempt(store, authorization.deployment_id)
        if loaded != requested:
            _invalid_state()
        return requested, True
    except CoreRestartError:
        raise
    except (StateError, sqlite3.Error):
        _invalid_state()


def _record_acknowledged(
    store: StateStore,
    authorization: PostApplyActivationAuthorization,
    *,
    when: datetime,
) -> CoreRestartAttempt:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _validate_authorization(store, authorization)
            existing = load_core_restart_attempt(store, authorization.deployment_id)
            if existing is None or existing.phase != "request_started":
                _invalid_state()
            replacement = CoreRestartAttempt.create(
                authorization, "request_acknowledged", existing.started_at, when
            )
            result = db.execute(
                "UPDATE core_restart_attempt SET phase = ?, updated_at = ?, record_sha256 = ? "
                "WHERE deployment_id = ? AND phase = 'request_started' AND record_sha256 = ?",
                (
                    replacement.phase,
                    replacement.updated_at.isoformat(),
                    replacement.record_sha256,
                    replacement.deployment_id,
                    existing.record_sha256,
                ),
            )
            if result.rowcount != 1:
                _invalid_state()
        loaded = load_core_restart_attempt(store, authorization.deployment_id)
        if loaded != replacement:
            _invalid_state()
        return replacement
    except CoreRestartError:
        raise
    except (StateError, sqlite3.Error):
        _invalid_state()


def _validate_authorization(
    store: StateStore, authorization: PostApplyActivationAuthorization
) -> None:
    if type(store) is not StateStore or type(authorization) is not PostApplyActivationAuthorization:
        raise CoreRestartError("Core restart authorization is invalid")
    try:
        loaded = load_post_apply_activation_authorization(store, authorization.deployment_id)
    except Exception:
        raise CoreRestartError("Core restart authorization is invalid") from None
    if loaded is None or loaded != authorization or authorization.action != "restart_core":
        raise CoreRestartError("Core restart authorization is invalid")


def _validate_response(response: CoreRestartResponse, max_response_bytes: int) -> None:
    if (
        type(response) is not CoreRestartResponse
        or response.status != 200
        or _media_type(response.content_type) != "application/json"
        or len(response.body) > max_response_bytes
    ):
        raise CoreRestartError("Core restart response is invalid")
    try:
        payload = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CoreRestartError("Core restart response is invalid") from None
    if payload != {"result": "ok", "data": {}}:
        raise CoreRestartError("Core restart response is invalid")


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
) -> CoreRestartResponse:
    if method != "POST" or url != _RESTART_URL or body != _REQUEST_BODY:
        raise CoreRestartError("Core restart request boundary is invalid")
    connection = http.client.HTTPConnection("supervisor", 80, timeout=timeout_seconds)
    try:
        connection.request("POST", _RESTART_PATH, body=body, headers=dict(headers))
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "") or ""
        content_length = response.getheader("Content-Length")
        if content_length is not None and int(content_length) > max_response_bytes:
            raise CoreRestartError("Core restart response exceeds size limit")
        response_body = response.read(max_response_bytes + 1)
        status = int(response.status)
    except CoreRestartError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        raise CoreRestartError("Core restart request failed") from None
    finally:
        connection.close()
    return CoreRestartResponse(status, content_type, response_body)


def _resolve_token(token: str | None) -> str:
    candidate = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if (
        not isinstance(candidate, str)
        or not candidate.strip()
        or candidate != candidate.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate)
    ):
        raise CoreRestartError("Supervisor API credential is invalid")
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
        raise CoreRestartError("Core restart transport limits are invalid")


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


def _construct_attempt(values: tuple[object, ...]) -> CoreRestartAttempt:
    if len(values) != 6:
        _invalid_state()
    result = object.__new__(CoreRestartAttempt)
    for name, value in zip(CoreRestartAttempt.__slots__, values, strict=True):
        object.__setattr__(result, name, value)
    return result


def _timestamp(value: datetime | None = None) -> datetime:
    when = datetime.now(UTC) if value is None else value
    if not _aware(when):
        raise CoreRestartError("Core restart timestamp is invalid")
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
    raise CoreRestartError("Core restart state is invalid") from None
