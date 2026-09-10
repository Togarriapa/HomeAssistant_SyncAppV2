"""Bounded read-only collection of Home Assistant Core and Supervisor logs."""

from __future__ import annotations

import hashlib
import http.client
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from .log_artifact import LogRecord

_SUPERVISOR_ROOT: Final = "http://supervisor"
_SOURCES: Final[tuple[tuple[str, str], ...]] = (
    ("home-assistant", f"{_SUPERVISOR_ROOT}/core/logs?lines=2000&no_colors"),
    ("supervisor", f"{_SUPERVISOR_ROOT}/supervisor/logs?lines=2000&no_colors"),
)
_ALLOWED_PATHS: Final = {
    f"{_SUPERVISOR_ROOT}/core/logs?lines=2000&no_colors": "/core/logs?lines=2000&no_colors",
    f"{_SUPERVISOR_ROOT}/supervisor/logs?lines=2000&no_colors": (
        "/supervisor/logs?lines=2000&no_colors"
    ),
}
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024
_MAX_LINE_BYTES: Final = 1024 * 1024


class SupervisorLogError(RuntimeError):
    """Required Supervisor-managed logs could not be collected safely."""


@dataclass(frozen=True, slots=True)
class SupervisorLogResponse:
    """Bounded transport response used by the collector and tests."""

    status: int
    content_type: str
    body: bytes


SupervisorLogTransport = Callable[
    [str, str, Mapping[str, str], float, int], SupervisorLogResponse
]


def collect_supervisor_logs(
    *,
    token: str | None = None,
    reference_time: datetime,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    transport: SupervisorLogTransport | None = None,
) -> tuple[LogRecord, ...]:
    """Collect bounded Core and Supervisor log snapshots through supported REST APIs."""
    bearer = _resolve_token(token)
    reference = _normalize_reference_time(reference_time)
    _validate_limits(timeout_seconds, max_response_bytes)
    sender = transport or _default_transport

    records: list[LogRecord] = []
    for category, url in _SOURCES:
        body = _request_text(
            category=category,
            url=url,
            token=bearer,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            transport=sender,
        )
        records.extend(_records_from_body(category, body, reference))
    return tuple(records)


def _request_text(
    *,
    category: str,
    url: str,
    token: str,
    timeout_seconds: float,
    max_response_bytes: int,
    transport: SupervisorLogTransport,
) -> bytes:
    headers = {
        "Accept": "text/plain",
        "Authorization": f"Bearer {token}",
    }
    try:
        response = transport("GET", url, headers, timeout_seconds, max_response_bytes)
    except Exception:
        raise SupervisorLogError(f"{category} log request failed") from None
    if type(response) is not SupervisorLogResponse:
        raise SupervisorLogError(f"{category} log response is invalid")
    if response.status != 200:
        raise SupervisorLogError(f"{category} log request failed")
    if _media_type(response.content_type) not in {"text/plain", "text/x-log"}:
        raise SupervisorLogError(f"{category} log response is not text")
    if len(response.body) > max_response_bytes:
        raise SupervisorLogError(f"{category} log response exceeds size limit")
    return response.body


def _records_from_body(category: str, body: bytes, reference: datetime) -> list[LogRecord]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise SupervisorLogError(f"{category} log response is not valid UTF-8") from None
    if "\x00" in text:
        raise SupervisorLogError(f"{category} log response contains invalid text")

    records: list[LogRecord] = []
    for index, line in enumerate(text.splitlines()):
        encoded = line.encode("utf-8")
        if len(encoded) > _MAX_LINE_BYTES:
            raise SupervisorLogError(f"{category} log record exceeds size limit")
        digest = hashlib.sha256(
            category.encode() + b"\x00" + str(index).encode() + b"\x00" + encoded
        ).hexdigest()
        records.append(
            LogRecord(
                category=category,
                record_id=digest,
                timestamp=reference,
                message=line,
            )
        )
    return records


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> SupervisorLogResponse:
    path = _ALLOWED_PATHS.get(url)
    if method != "GET" or path is None:
        raise SupervisorLogError("Supervisor log request boundary is invalid")

    connection = http.client.HTTPConnection("supervisor", 80, timeout=timeout_seconds)
    try:
        connection.request("GET", path, headers=dict(headers))
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "") or ""
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > max_response_bytes:
                    raise SupervisorLogError("Supervisor log response exceeds size limit")
            except ValueError:
                raise SupervisorLogError("Supervisor log response metadata is invalid") from None
        body = response.read(max_response_bytes + 1)
        status = int(response.status)
    except SupervisorLogError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        raise SupervisorLogError("Supervisor log request failed") from None
    finally:
        connection.close()
    return SupervisorLogResponse(status=status, content_type=content_type, body=body)


def _resolve_token(token: str | None) -> str:
    candidate = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
    if not isinstance(candidate, str) or not candidate.strip() or candidate != candidate.strip():
        raise SupervisorLogError("Supervisor API credential is unavailable")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        raise SupervisorLogError("Supervisor API credential is invalid")
    return candidate


def _normalize_reference_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SupervisorLogError("log collection reference time must be timezone-aware")
    return value.astimezone(UTC)


def _validate_limits(timeout_seconds: float, max_response_bytes: int) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
        raise SupervisorLogError("Supervisor log timeout is invalid")
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise SupervisorLogError("Supervisor log timeout is invalid")
    if type(max_response_bytes) is not int or not 0 < max_response_bytes <= 16 * 1024 * 1024:
        raise SupervisorLogError("Supervisor log response limit is invalid")


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()
