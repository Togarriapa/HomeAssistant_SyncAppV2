"""App lifecycle with fail-closed Repo B trust and same-owner Retrigger IPC."""

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from . import __version__
from .config import Config, ConfigError, load_config
from .github_repo import RepositoryVerificationError, fetch_and_verify_private_repository
from .retrigger_cycle import RetriggerCycleError, run_retrigger_cycle
from .retrigger_ipc import (
    RetriggerIPCError,
    RetriggerRequest,
    RetriggerServer,
    request_retrigger_once,
    retrigger_socket_path,
)
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


def _work_roots(data_dir: Path, home_assistant_root: Path) -> tuple[Path, ...]:
    protected = (data_dir / "syncapp").resolve(strict=True)
    home = home_assistant_root.resolve(strict=True)
    work = protected / "work"
    if protected == home or protected in home.parents or home in protected.parents:
        raise RetriggerCycleError("Retrigger protected work root overlaps Home Assistant source")
    return (
        work / "main-snapshots",
        work / "main-workspaces",
        work / "database-staging",
        work / "database-snapshots",
        work / "database-workspaces",
        work / "runtime-staging",
        work / "runtime-snapshots",
        work / "runtime-workspaces",
        work / "log-artifacts",
        work / "log-snapshots",
        work / "log-workspaces",
    )


def _handle_retrigger_request(
    store: StateStore,
    config: Config,
    data_dir: Path,
    request: RetriggerRequest,
) -> str:
    """Execute one bounded outbound cycle while the service retains state ownership."""
    if config.repo_b is None or config.github_token is None:
        return "configuration_invalid"
    try:
        expected_id = store.repository_id(config.repo_b)
        if expected_id is None:
            return "repo_b_untrusted"
        identity = fetch_and_verify_private_repository(
            config.repo_b,
            config.github_token,
            expected_id=expected_id,
        )
        store.bind_repository(config.repo_b, identity.repository_id)
        (
            snapshot_staging_root,
            local_workspace_root,
            database_staging_root,
            database_snapshot_root,
            database_workspace_root,
            runtime_staging_root,
            runtime_snapshot_root,
            runtime_workspace_root,
            log_artifact_root,
            log_snapshot_root,
            log_workspace_root,
        ) = _work_roots(data_dir, request.home_assistant_root)
        run_retrigger_cycle(
            store,
            request.home_assistant_root,
            snapshot_staging_root,
            local_workspace_root,
            request.recorder_database,
            database_staging_root,
            database_snapshot_root,
            database_workspace_root,
            runtime_staging_root,
            runtime_snapshot_root,
            runtime_workspace_root,
            config.repo_b,
            config.github_token,
            core_token=os.environ.get("SUPERVISOR_TOKEN"),
            log_artifact_root=log_artifact_root,
            log_snapshot_root=log_snapshot_root,
            log_workspace_root=log_workspace_root,
        )
    except RepositoryVerificationError:
        return "repo_b_untrusted"
    except (RetriggerCycleError, OSError):
        return "cycle_failed"
    except StateError:
        return "internal_error"
    return "completed"


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
        socket_path = retrigger_socket_path(data_dir.resolve(strict=True))
        next_status = time.monotonic() + config.status_interval_seconds

        with RetriggerServer(socket_path) as retrigger_server:
            fields = {**asdict(boot), "version": __version__, "mode": "passive"}
            level = "warning" if boot.interrupted_run_id else "info"
            if config.log_level == "info" or (config.log_level == "warning" and level == "warning"):
                emit("service_started", level=level, **fields)

            while not stop.requested:
                try:
                    retrigger_server.serve_once(
                        lambda request: _handle_retrigger_request(store, config, data_dir, request),
                        timeout_seconds=0.25,
                    )
                except RetriggerIPCError:
                    if stop.requested:
                        break
                    raise
                now = time.monotonic()
                if now >= next_status:
                    if config.log_level == "info":
                        emit("service_idle", run_id=boot.run_id, mode="passive")
                    next_status = now + config.status_interval_seconds

        store.finish_run()
        if config.log_level == "info":
            emit("service_stopped", run_id=boot.run_id)


def _run_retrigger_client(
    data_dir: Path,
    home_assistant_root: Path | None,
    recorder_database: Path | None,
) -> int:
    if home_assistant_root is None or recorder_database is None:
        emit("retrigger_failed", level="error", reason="source_paths_required")
        return 2
    try:
        socket_path = retrigger_socket_path(data_dir.resolve(strict=True))
        request_retrigger_once(socket_path, home_assistant_root, recorder_database)
    except (RetriggerIPCError, OSError):
        emit("retrigger_failed", level="error", reason="request_failed")
        return 6
    emit("retrigger_completed", mode="one_shot")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Home Assistant SyncApp foundation service")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--retrigger-once", action="store_true")
    parser.add_argument("--home-assistant-root", type=Path)
    parser.add_argument("--recorder-database", type=Path)
    args = parser.parse_args()

    if args.retrigger_once:
        return _run_retrigger_client(
            args.data_dir,
            args.home_assistant_root,
            args.recorder_database,
        )
    if args.home_assistant_root is not None or args.recorder_database is not None:
        emit("service_failed", level="error", reason="unexpected_source_paths")
        return 2

    stop = Shutdown()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
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
    except RetriggerIPCError:
        emit("service_failed", level="error", reason="retrigger_ipc_unavailable")
        return 6
    except Exception:
        emit("service_failed", level="error", reason="internal_error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
