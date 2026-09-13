"""Supervise the state-owning service and its recurring Retrigger dispatcher."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from .config import Config, ConfigError, load_config
from .retrigger_ipc import RetriggerIPCError, request_retrigger_once, retrigger_socket_path
from .retrigger_schedule import RetriggerSchedule, RetriggerScheduleError

_DATA_DIR = Path("/data")
_HOME_ASSISTANT_ROOT = Path("/homeassistant")
_POLL_SECONDS = 0.25
_RETRIGGER_SETUP_FAILURE = 14


def _emit(event: str, *, level: str = "info", **fields: object) -> None:
    """Emit only scheduler-owned sanitized fields."""
    print(
        json.dumps(
            {"timestamp": datetime.now(UTC).isoformat(), "level": level, "event": event, **fields},
            separators=(",", ":"),
        ),
        flush=True,
    )


def _load_scheduler_config(data_dir: Path) -> Config | None:
    """Read the same validated options as the service without exposing their contents."""
    try:
        return load_config(data_dir / "options.json")
    except ConfigError:
        # The state-owning service remains authoritative for the public failure code/log.
        return None


def _build_schedule(config: Config | None, data_dir: Path) -> RetriggerSchedule | None:
    """Build recovery scheduling only when Repo B is configured for real work."""
    if config is None or config.repo_b is None or config.github_token is None:
        return None
    socket_path = retrigger_socket_path(data_dir.resolve(strict=True))
    recorder = (
        None if config.recorder_database_path is None else Path(config.recorder_database_path)
    )

    def dispatch() -> str:
        request_retrigger_once(
            socket_path,
            _HOME_ASSISTANT_ROOT,
            recorder,
        )
        return "completed"

    return RetriggerSchedule(
        interval_seconds=config.retrigger_interval_seconds,
        run_cycle=dispatch,
    )


def _service_command(arguments: list[str]) -> list[str]:
    return [sys.executable, "-m", "ha_syncapp", *arguments]


def run(arguments: list[str] | None = None, *, data_dir: Path = _DATA_DIR) -> int:
    """Run the service and periodically request bounded recovery through protected IPC."""
    forwarded = list(sys.argv[1:] if arguments is None else arguments)
    if forwarded:
        # Preserve administrative/one-shot CLI behavior exactly; scheduled recovery is
        # only part of the normal long-running App lifecycle.
        os.execv(sys.executable, _service_command(forwarded))
        raise AssertionError("execv returned unexpectedly")

    config = _load_scheduler_config(data_dir)
    child = subprocess.Popen(_service_command([]))
    stopping = False

    def request_stop(signum: int, frame: FrameType | None) -> None:
        nonlocal stopping
        del signum, frame
        stopping = True
        if child.poll() is None:
            child.terminate()

    previous_handlers: dict[
        signal.Signals,
        signal.Handlers | int | Callable[[int, FrameType | None], Any] | None,
    ] = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, request_stop)

    schedule: RetriggerSchedule | None = None
    setup_failed = False
    try:
        try:
            schedule = _build_schedule(config, data_dir)
        except (OSError, RetriggerIPCError, RetriggerScheduleError):
            _emit("retrigger_schedule_failed", level="error", reason="setup_failed")
            setup_failed = True
            if child.poll() is None:
                child.terminate()
        if setup_failed:
            child.wait()
            return _RETRIGGER_SETUP_FAILURE

        if schedule is not None and config is not None:
            schedule.start(time.monotonic())
            _emit(
                "retrigger_schedule_started",
                interval_seconds=config.retrigger_interval_seconds,
            )

        while child.poll() is None:
            if stopping:
                break
            if schedule is not None:
                try:
                    result = schedule.tick(time.monotonic())
                    if result == "completed":
                        _emit("retrigger_schedule_completed")
                except RetriggerIPCError:
                    _emit("retrigger_schedule_failed", level="warning", reason="request_failed")
                except RetriggerScheduleError:
                    _emit("retrigger_schedule_failed", level="error", reason="clock_invalid")
                    child.terminate()
                    stopping = True
                    break
            time.sleep(_POLL_SECONDS)
    finally:
        if schedule is not None:
            schedule.stop()
        if child.poll() is None:
            child.terminate()
        return_code = child.wait() if not setup_failed else _RETRIGGER_SETUP_FAILURE
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)

    return return_code


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
