"""App lifecycle with fail-closed Repo B trust establishment."""

import argparse
import json
import signal
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from . import __version__
from .config import ConfigError, load_config
from .github_repo import RepositoryVerificationError, fetch_and_verify_private_repository
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


def run(data_dir: Path, stop: Shutdown) -> None:
    config = load_config(data_dir / "options.json")
    with StateStore(data_dir) as store:
        if config.repo_b is not None and config.github_token is not None:
            expected_id = store.repository_id(config.repo_b)
            identity = fetch_and_verify_private_repository(
                config.repo_b,
                config.github_token,
                expected_id=expected_id,
            )
            store.bind_repository(config.repo_b, identity.repository_id)
        boot = store.start_run()
        fields = {**asdict(boot), "version": __version__, "mode": "passive"}
        level = "warning" if boot.interrupted_run_id else "info"
        if config.log_level == "info" or (config.log_level == "warning" and level == "warning"):
            emit("service_started", level=level, **fields)
        while not stop.wait(config.status_interval_seconds):
            if config.log_level == "info":
                emit("service_idle", run_id=boot.run_id, mode="passive")
        store.finish_run()
        if config.log_level == "info":
            emit("service_stopped", run_id=boot.run_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Home Assistant SyncApp foundation service")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    args = parser.parse_args()
    stop = Shutdown()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.requested = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)
    try:
        run(args.data_dir, stop)
    except ConfigError:
        emit("service_failed", level="error", reason="configuration_invalid")
        return 2
    except AlreadyRunning:
        emit("service_failed", level="error", reason="already_running")
        return 3
    except RepositoryVerificationError:
        emit("service_failed", level="error", reason="repo_b_untrusted")
        return 5
    except StateError:
        emit("service_failed", level="error", reason="state_unavailable")
        return 4
    except Exception:
        emit("service_failed", level="error", reason="internal_error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
