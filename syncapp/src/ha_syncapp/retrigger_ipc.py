"""Protected same-owner IPC for one bounded Retrigger request."""

from __future__ import annotations

import json
import os
import socket
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_PROTOCOL_VERSION: Final = 1
_MAX_MESSAGE_BYTES: Final = 8192
_MAX_PATH_CHARS: Final = 4096
_SOCKET_NAME: Final = "retrigger.sock"


class RetriggerIPCError(RuntimeError):
    """A Retrigger IPC request could not be exchanged safely."""


@dataclass(frozen=True, slots=True)
class RetriggerRequest:
    """Explicit live-source paths supplied by a cron-invoked client."""

    home_assistant_root: Path
    recorder_database: Path


def retrigger_socket_path(data_dir: Path) -> Path:
    """Return the socket location inside the StateStore-owned private directory."""
    if not isinstance(data_dir, Path) or not data_dir.is_absolute():
        raise RetriggerIPCError("Retrigger data directory is invalid")
    path = data_dir / "syncapp" / _SOCKET_NAME
    if len(os.fsencode(path)) >= 100:
        raise RetriggerIPCError("Retrigger socket path is too long")
    return path


class RetriggerServer:
    """Synchronous Unix-socket server owned by the StateStore-owning process."""

    def __init__(self, socket_path: Path) -> None:
        self._path = socket_path
        self._socket: socket.socket | None = None

    def __enter__(self) -> RetriggerServer:
        if self._socket is not None:
            raise RetriggerIPCError("Retrigger server cannot be reopened")
        _prepare_socket_path(self._path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(os.fspath(self._path))
            os.chmod(self._path, 0o600, follow_symlinks=False)
            info = self._path.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise RetriggerIPCError("Retrigger socket is unsafe")
            server.listen(1)
            self._socket = server
            return self
        except BaseException:
            server.close()
            _remove_owned_socket(self._path)
            raise

    def __exit__(self, *args: object) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        _remove_owned_socket(self._path)

    def serve_once(
        self,
        handler: Callable[[RetriggerRequest], str],
        *,
        timeout_seconds: float = 0.25,
    ) -> bool:
        """Handle at most one request and return whether a client was served."""
        server = self._socket
        if server is None:
            raise RetriggerIPCError("Retrigger server is not open")
        _validate_timeout(timeout_seconds)
        server.settimeout(timeout_seconds)
        try:
            connection, _ = server.accept()
        except TimeoutError:
            return False
        except OSError:
            raise RetriggerIPCError("Retrigger server accept failed") from None
        with connection:
            request = _receive_request(connection, timeout_seconds=max(timeout_seconds, 1.0))
            try:
                reason = handler(request)
            except Exception:
                reason = "internal_error"
            if reason == "completed":
                response = {"version": _PROTOCOL_VERSION, "status": "completed"}
            else:
                response = {
                    "version": _PROTOCOL_VERSION,
                    "status": "failed",
                    "reason": _sanitize_reason(reason),
                }
            _send_json(connection, response)
        return True


def request_retrigger_once(
    socket_path: Path,
    home_assistant_root: Path,
    recorder_database: Path,
    *,
    timeout_seconds: float = 300.0,
) -> None:
    """Ask the state-owning service to execute exactly one bounded Retrigger cycle."""
    _validate_timeout(timeout_seconds)
    request = RetriggerRequest(home_assistant_root, recorder_database)
    payload = _request_payload(request)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout_seconds)
        client.connect(os.fspath(socket_path))
        _send_json(client, payload)
        response = _receive_json(client, timeout_seconds)
    except RetriggerIPCError:
        raise
    except (OSError, TimeoutError):
        raise RetriggerIPCError("Retrigger service is unavailable") from None
    finally:
        client.close()

    if not isinstance(response, dict) or response.get("version") != _PROTOCOL_VERSION:
        raise RetriggerIPCError("Retrigger response is invalid")
    if response == {"version": _PROTOCOL_VERSION, "status": "completed"}:
        return
    if (
        response.get("status") == "failed"
        and isinstance(response.get("reason"), str)
        and set(response) == {"version", "status", "reason"}
    ):
        raise RetriggerIPCError(f"Retrigger request failed: {response['reason']}")
    raise RetriggerIPCError("Retrigger response is invalid")


def _request_payload(request: RetriggerRequest) -> dict[str, object]:
    if type(request) is not RetriggerRequest:
        raise RetriggerIPCError("Retrigger request is invalid")
    home = _validate_request_path(request.home_assistant_root)
    database = _validate_request_path(request.recorder_database)
    return {
        "version": _PROTOCOL_VERSION,
        "command": "retrigger_once",
        "home_assistant_root": home,
        "recorder_database": database,
    }


def _receive_request(connection: socket.socket, timeout_seconds: float) -> RetriggerRequest:
    value = _receive_json(connection, timeout_seconds)
    if not isinstance(value, dict) or set(value) != {
        "version",
        "command",
        "home_assistant_root",
        "recorder_database",
    }:
        raise RetriggerIPCError("Retrigger request is invalid")
    if value.get("version") != _PROTOCOL_VERSION or value.get("command") != "retrigger_once":
        raise RetriggerIPCError("Retrigger request is invalid")
    home = value.get("home_assistant_root")
    database = value.get("recorder_database")
    if not isinstance(home, str) or not isinstance(database, str):
        raise RetriggerIPCError("Retrigger request is invalid")
    return RetriggerRequest(
        Path(_validate_path_text(home)),
        Path(_validate_path_text(database)),
    )


def _validate_request_path(path: Path) -> str:
    if not isinstance(path, Path):
        raise RetriggerIPCError("Retrigger source path is invalid")
    return _validate_path_text(os.fspath(path))


def _validate_path_text(value: str) -> str:
    if (
        not value
        or len(value) > _MAX_PATH_CHARS
        or not value.startswith("/")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise RetriggerIPCError("Retrigger source path is invalid")
    return value


def _receive_json(connection: socket.socket, timeout_seconds: float) -> object:
    connection.settimeout(timeout_seconds)
    buffer = bytearray()
    while True:
        try:
            chunk = connection.recv(min(4096, _MAX_MESSAGE_BYTES + 1 - len(buffer)))
        except (OSError, TimeoutError):
            raise RetriggerIPCError("Retrigger IPC receive failed") from None
        if not chunk:
            raise RetriggerIPCError("Retrigger IPC message is incomplete")
        buffer.extend(chunk)
        if len(buffer) > _MAX_MESSAGE_BYTES:
            raise RetriggerIPCError("Retrigger IPC message exceeds size limit")
        if b"\n" in buffer:
            line, separator, remainder = bytes(buffer).partition(b"\n")
            if not separator or remainder:
                raise RetriggerIPCError("Retrigger IPC framing is invalid")
            break
    try:
        return json.loads(
            line.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        raise RetriggerIPCError("Retrigger IPC message is invalid") from None


def _send_json(connection: socket.socket, payload: dict[str, object]) -> None:
    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise RetriggerIPCError("Retrigger IPC message is invalid") from None
    if len(encoded) > _MAX_MESSAGE_BYTES:
        raise RetriggerIPCError("Retrigger IPC message exceeds size limit")
    try:
        connection.sendall(encoded)
    except OSError:
        raise RetriggerIPCError("Retrigger IPC send failed") from None


def _prepare_socket_path(path: Path) -> None:
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except OSError:
        raise RetriggerIPCError("Retrigger socket directory is unavailable") from None
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise RetriggerIPCError("Retrigger socket directory is unsafe")
    if os.path.lexists(path):
        _remove_owned_socket(path)


def _remove_owned_socket(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise RetriggerIPCError("Retrigger socket path is unsafe")
        path.unlink()
    except OSError:
        raise RetriggerIPCError("Retrigger socket cleanup failed") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _sanitize_reason(reason: str) -> str:
    allowed = {
        "completed",
        "configuration_invalid",
        "repo_b_untrusted",
        "source_invalid",
        "cycle_failed",
        "internal_error",
    }
    return reason if reason in allowed else "internal_error"


def _validate_timeout(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0 or value > 600:
        raise RetriggerIPCError("Retrigger timeout is invalid")
