"""Signal-safe lifecycle and serialized worker with an admin ingress control panel."""

import argparse
import json
import os
import signal
import sys
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from . import __version__
from .application import Application
from .config import ConfigError, load_config
from .control import make_server
from .homeassistant import HomeAssistant
from .journal import Journal
from .state import AlreadyRunning, StateError, StateStore


def emit(event: str, *, level: str = "info", **fields: object) -> None:
    """Emit explicitly selected fields, never exception text or raw options."""
    print(
        json.dumps(
            {"timestamp": datetime.now(UTC).isoformat(), "level": level, "event": event, **fields},
            separators=(",", ":"),
        ),
        flush=True,
    )


class Shutdown:
    """A signal-safe flag; handlers must not acquire threading locks."""

    def __init__(self) -> None:
        self.requested = False

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.25))
        return self.requested


def run(data_dir: Path, stop: Shutdown, config_dir: Path = Path("/homeassistant")) -> None:
    config = load_config(data_dir / "options.json")
    with StateStore(data_dir) as store:
        boot = store.start_run()
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        mode = "setup" if token else "passive"
        application = None
        server = None
        thread = None
        if token:
            application = Application(
                config,
                data_dir / "syncapp",
                config_dir,
                Journal(store.connection),
                HomeAssistant(token),
            )
            # Only the Supervisor ingress peer is accepted by the HTTP handler.
            server = make_server(application.control, ("0.0.0.0", 8099))  # nosec B104
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
        fields = {**asdict(boot), "version": __version__, "mode": mode}
        level = "warning" if boot.interrupted_run_id else "info"
        if config.log_level == "info" or (config.log_level == "warning" and level == "warning"):
            emit("service_started", level=level, **fields)
        next_status = time.monotonic() + config.status_interval_seconds
        try:
            while not stop.wait(0.25 if application else config.status_interval_seconds):
                if application:
                    application.step()
                if time.monotonic() >= next_status:
                    if config.log_level == "info":
                        emit("service_idle", run_id=boot.run_id, mode=mode)
                    next_status = time.monotonic() + config.status_interval_seconds
        finally:
            if server:
                server.shutdown()
                server.server_close()
            if thread:
                thread.join(timeout=5)
        store.finish_run()
        if config.log_level == "info":
            emit("service_stopped", run_id=boot.run_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Home Assistant SyncApp V2")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--config-dir", type=Path, default=Path("/homeassistant"))
    args = parser.parse_args()
    stop = Shutdown()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.requested = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)
    try:
        run(args.data_dir, stop, args.config_dir)
    except ConfigError:
        emit("service_failed", level="error", reason="configuration_invalid")
        return 2
    except AlreadyRunning:
        emit("service_failed", level="error", reason="already_running")
        return 3
    except StateError:
        emit("service_failed", level="error", reason="state_unavailable")
        return 4
    except Exception:
        # Unexpected errors also fail closed without disclosing private data.
        emit("service_failed", level="error", reason="internal_error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
