from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from threading import Thread

import pytest
from ha_syncapp.retrigger_ipc import (
    RetriggerIPCError,
    RetriggerRequest,
    RetriggerServer,
    request_retrigger_once,
    retrigger_socket_path,
)


def _private_root(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    root = data / "syncapp"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return data, root


def test_client_and_state_owner_exchange_only_explicit_source_paths(tmp_path: Path) -> None:
    data, _ = _private_root(tmp_path)
    path = retrigger_socket_path(data.resolve())
    seen: list[RetriggerRequest] = []

    with RetriggerServer(path) as server:
        thread = Thread(
            target=lambda: server.serve_once(
                lambda request: seen.append(request) or "completed",
                timeout_seconds=5,
            )
        )
        thread.start()
        request_retrigger_once(
            path,
            Path("/homeassistant"),
            Path("/homeassistant/home-assistant_v2.db"),
            timeout_seconds=5,
        )
        thread.join(timeout=5)

    assert thread.is_alive() is False
    assert seen == [
        RetriggerRequest(
            Path("/homeassistant"),
            Path("/homeassistant/home-assistant_v2.db"),
        )
    ]
    assert path.exists() is False


def test_server_returns_only_sanitized_failure_reason(tmp_path: Path) -> None:
    data, _ = _private_root(tmp_path)
    path = retrigger_socket_path(data.resolve())

    with RetriggerServer(path) as server:
        thread = Thread(
            target=lambda: server.serve_once(
                lambda request: "ghp_secret_nested_detail",
                timeout_seconds=5,
            )
        )
        thread.start()
        with pytest.raises(RetriggerIPCError) as exc_info:
            request_retrigger_once(
                path,
                Path("/homeassistant"),
                Path("/homeassistant/home-assistant_v2.db"),
                timeout_seconds=5,
            )
        thread.join(timeout=5)

    assert str(exc_info.value) == "Retrigger request failed: internal_error"
    assert "ghp_secret_nested_detail" not in str(exc_info.value)


def test_server_socket_is_private_and_replaces_only_owned_stale_socket(tmp_path: Path) -> None:
    data, _ = _private_root(tmp_path)
    path = retrigger_socket_path(data.resolve())
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(os.fspath(path))
    stale.close()

    with RetriggerServer(path):
        assert path.is_socket()
        assert path.stat().st_mode & 0o777 == 0o600

    assert path.exists() is False


def test_server_refuses_non_socket_collision(tmp_path: Path) -> None:
    data, _ = _private_root(tmp_path)
    path = retrigger_socket_path(data.resolve())
    path.write_text("do not remove")

    with pytest.raises(RetriggerIPCError, match="socket path is unsafe"):
        RetriggerServer(path).__enter__()

    assert path.read_text() == "do not remove"


def test_server_refuses_symlink_collision(tmp_path: Path) -> None:
    data, root = _private_root(tmp_path)
    path = retrigger_socket_path(data.resolve())
    target = root / "target"
    target.write_text("protected")
    path.symlink_to(target)

    with pytest.raises(RetriggerIPCError, match="socket path is unsafe"):
        RetriggerServer(path).__enter__()

    assert path.is_symlink()
    assert target.read_text() == "protected"


def test_client_rejects_relative_or_control_character_paths_before_connecting(
    tmp_path: Path,
) -> None:
    data, _ = _private_root(tmp_path)
    path = retrigger_socket_path(data.resolve())

    for home in (Path("relative"), Path("/homeassistant\nother")):
        with pytest.raises(RetriggerIPCError, match="source path is invalid"):
            request_retrigger_once(
                path,
                home,
                Path("/homeassistant/home-assistant_v2.db"),
            )


def test_server_rejects_duplicate_keys_and_does_not_call_handler(tmp_path: Path) -> None:
    data, _ = _private_root(tmp_path)
    path = retrigger_socket_path(data.resolve())
    called = False
    server_error: list[BaseException] = []

    def handler(request: RetriggerRequest) -> str:
        nonlocal called
        called = True
        return "completed"

    def serve(server: RetriggerServer) -> None:
        try:
            server.serve_once(handler, timeout_seconds=5)
        except BaseException as exc:
            server_error.append(exc)

    with RetriggerServer(path) as server:
        thread = Thread(target=serve, args=(server,))
        thread.start()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(os.fspath(path))
        client.sendall(
            b'{"version":1,"version":1,"command":"retrigger_once",'
            b'"home_assistant_root":"/homeassistant",'
            b'"recorder_database":"/homeassistant/db"}\n'
        )
        client.close()
        thread.join(timeout=5)

    assert called is False
    assert len(server_error) == 1
    assert isinstance(server_error[0], RetriggerIPCError)


def test_protocol_payload_contains_no_credential_fields(tmp_path: Path) -> None:
    data, _ = _private_root(tmp_path)
    path = retrigger_socket_path(data.resolve())
    captured: list[dict[str, object]] = []

    raw_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw_server.bind(os.fspath(path))
    raw_server.listen(1)

    def receive() -> None:
        connection, _ = raw_server.accept()
        with connection:
            message = connection.recv(8192).decode("utf-8")
            captured.append(json.loads(message))
            connection.sendall(b'{"status":"completed","version":1}\n')

    thread = Thread(target=receive)
    thread.start()
    try:
        request_retrigger_once(
            path,
            Path("/homeassistant"),
            Path("/homeassistant/home-assistant_v2.db"),
            timeout_seconds=5,
        )
    finally:
        thread.join(timeout=5)
        raw_server.close()
        path.unlink()

    assert captured == [
        {
            "version": 1,
            "command": "retrigger_once",
            "home_assistant_root": "/homeassistant",
            "recorder_database": "/homeassistant/home-assistant_v2.db",
        }
    ]
    assert not ({"github_token", "supervisor_token", "repo_b"} & set(captured[0]))
