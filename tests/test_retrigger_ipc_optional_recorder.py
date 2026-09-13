"""Optional Recorder source transport for recurring Retrigger recovery."""

from __future__ import annotations

from pathlib import Path
from threading import Thread

from ha_syncapp.retrigger_ipc import (
    RetriggerRequest,
    RetriggerServer,
    request_retrigger_once,
    retrigger_socket_path,
)


def test_retrigger_ipc_preserves_absent_recorder_source(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    protected = data / "syncapp"
    protected.mkdir(mode=0o700)
    protected.chmod(0o700)
    socket_path = retrigger_socket_path(data.resolve())
    seen: list[RetriggerRequest] = []

    with RetriggerServer(socket_path) as server:
        thread = Thread(
            target=lambda: server.serve_once(
                lambda request: seen.append(request) or "completed",
                timeout_seconds=5,
            )
        )
        thread.start()
        request_retrigger_once(
            socket_path,
            Path("/homeassistant"),
            None,
            timeout_seconds=5,
        )
        thread.join(timeout=5)

    assert thread.is_alive() is False
    assert seen == [RetriggerRequest(Path("/homeassistant"), None)]
