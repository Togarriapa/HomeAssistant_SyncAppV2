"""Production owner service with recurring bounded Retrigger recovery."""

from __future__ import annotations

import os
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from . import __main__ as app
from .candidate_detection_service import CandidateDetectionService, CandidateDetectionServiceError
from .config import Config, load_config
from .database_sync_service import DatabaseSyncService, DatabaseSyncServiceError
from .github_repo import RepositoryVerificationError, fetch_and_verify_private_repository
from .local_change_service import LocalChangeService, LocalChangeServiceError
from .log_sync_service import LogSyncService, LogSyncServiceError
from .retrigger_cycle import RetriggerCycleError, run_retrigger_cycle
from .retrigger_ipc import RetriggerIPCError, RetriggerServer, retrigger_socket_path
from .retrigger_scheduler import RetriggerScheduler
from .runtime_event_bridge import RuntimeEventBridge, RuntimeEventBridgeError
from .state import StateError, StateStore

_HOME_ASSISTANT_ROOT = Path("/homeassistant")


def _scheduler_if_configured(store: StateStore, config: Config) -> RetriggerScheduler | None:
    """Create cadence only after Repo B has already been trusted by the owner service."""
    if config.repo_b is None or config.github_token is None:
        return None
    if store.repository_id(config.repo_b) is None:
        raise RetriggerCycleError("automatic Retrigger repository is not trusted")
    return RetriggerScheduler(interval_seconds=config.retrigger_interval_seconds)


def _run_automatic_retrigger_cycle(
    store: StateStore,
    config: Config,
    data_dir: Path,
    *,
    home_assistant_root: Path = _HOME_ASSISTANT_ROOT,
) -> str:
    """Run one sanitized bounded recovery cycle without administrative re-arming."""
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
        ) = app._work_roots(data_dir, home_assistant_root)
        recorder_database = (
            Path(config.recorder_database_path)
            if config.recorder_database_path is not None
            else None
        )
        run_retrigger_cycle(
            store,
            home_assistant_root,
            snapshot_staging_root,
            local_workspace_root,
            recorder_database,
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


def run(data_dir: Path, stop: app.Shutdown) -> None:
    """Own durable state and all periodic/event-driven work in one process."""
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
        scheduler = _scheduler_if_configured(store, config)
        boot = store.start_run()
        local_change_service: LocalChangeService | None = None
        database_sync_service: DatabaseSyncService | None = None
        runtime_bridge: RuntimeEventBridge | None = None
        log_sync_service: LogSyncService | None = None
        candidate_detection_service: CandidateDetectionService | None = None
        if not stop.requested:
            app._run_startup_local_if_configured(store, config, data_dir)
            if not stop.requested:
                local_change_service = app._local_change_service_if_configured(store, config, data_dir)
            if not stop.requested:
                app._run_startup_database_if_configured(store, config, data_dir)
            if not stop.requested:
                database_sync_service = app._database_sync_service_if_configured(store, config, data_dir)
            if not stop.requested:
                app._run_startup_runtime_if_configured(store, config, data_dir)
            if not stop.requested:
                runtime_bridge = app._runtime_event_bridge_if_configured(store, config, data_dir)
            if not stop.requested:
                log_sync_service = app._log_sync_service_if_configured(store, config, data_dir)
            if not stop.requested:
                candidate_detection_service = app._candidate_detection_service_if_configured(store, config)

        socket_path = retrigger_socket_path(data_dir.resolve(strict=True))
        next_status = time.monotonic() + config.status_interval_seconds
        mode = (
            "active"
            if scheduler is not None
            or local_change_service is not None
            or database_sync_service is not None
            or runtime_bridge is not None
            or log_sync_service is not None
            or candidate_detection_service is not None
            else "passive"
        )
        local_change_started = False
        database_sync_started = False
        runtime_bridge_started = False
        log_sync_started = False
        candidate_detection_started = False

        try:
            if not stop.requested and local_change_service is not None:
                local_change_service.start(time.monotonic())
                local_change_started = True
            if not stop.requested and database_sync_service is not None:
                database_sync_service.start(time.monotonic())
                database_sync_started = True
            if not stop.requested and runtime_bridge is not None:
                runtime_bridge.start()
                runtime_bridge_started = True
            if not stop.requested and log_sync_service is not None:
                log_sync_service.start(time.monotonic())
                log_sync_started = True
            if not stop.requested and candidate_detection_service is not None:
                candidate_detection_service.start(time.monotonic())
                candidate_detection_started = True
            if not stop.requested and scheduler is not None:
                scheduler.arm(time.monotonic())

            with RetriggerServer(socket_path) as retrigger_server:
                fields = {**asdict(boot), "version": app.__version__, "mode": mode}
                level = "warning" if boot.interrupted_run_id else "info"
                if config.log_level == "info" or (
                    config.log_level == "warning" and level == "warning"
                ):
                    app.emit("service_started", level=level, **fields)

                while not stop.requested:
                    try:
                        retrigger_server.serve_once(
                            lambda request: app._handle_retrigger_request(
                                store, config, data_dir, request
                            ),
                            timeout_seconds=0.25,
                        )
                    except RetriggerIPCError:
                        if stop.requested:
                            break
                        raise
                    if stop.requested:
                        break
                    now = time.monotonic()
                    if local_change_started and local_change_service is not None:
                        local_change_service.tick(now)
                    if database_sync_started and database_sync_service is not None:
                        database_sync_service.tick(now)
                    if runtime_bridge_started and runtime_bridge is not None:
                        runtime_bridge.tick()
                    if log_sync_started and log_sync_service is not None:
                        log_sync_service.tick(now)
                    if candidate_detection_started and candidate_detection_service is not None:
                        candidate_detection_service.tick(now)
                    if scheduler is not None and scheduler.due(now):
                        outcome = _run_automatic_retrigger_cycle(store, config, data_dir)
                        app.emit("retrigger_cycle_completed", mode="automatic", outcome=outcome)
                    if now >= next_status:
                        if config.log_level == "info":
                            app.emit("service_idle", run_id=boot.run_id, mode=mode)
                        next_status = now + config.status_interval_seconds
        except BaseException:
            if scheduler is not None:
                scheduler.disarm()
            if local_change_started and local_change_service is not None:
                with suppress(LocalChangeServiceError):
                    local_change_service.stop()
            if database_sync_started and database_sync_service is not None:
                with suppress(DatabaseSyncServiceError):
                    database_sync_service.stop()
            if runtime_bridge_started and runtime_bridge is not None:
                with suppress(RuntimeEventBridgeError):
                    runtime_bridge.stop()
            if log_sync_started and log_sync_service is not None:
                with suppress(LogSyncServiceError):
                    log_sync_service.stop()
            if candidate_detection_started and candidate_detection_service is not None:
                with suppress(CandidateDetectionServiceError):
                    candidate_detection_service.stop()
            raise

        if scheduler is not None:
            scheduler.disarm()
        if local_change_started and local_change_service is not None:
            local_change_service.stop()
        if database_sync_started and database_sync_service is not None:
            database_sync_service.stop()
        if runtime_bridge_started and runtime_bridge is not None:
            runtime_bridge.stop()
        if log_sync_started and log_sync_service is not None:
            log_sync_service.stop()
        if candidate_detection_started and candidate_detection_service is not None:
            candidate_detection_service.stop()
        store.finish_run()
        if config.log_level == "info":
            app.emit("service_stopped", run_id=boot.run_id)


def main() -> int:
    """Use the existing CLI/error contract with the recurring owner run implementation."""
    app.run = run
    return app.main()


if __name__ == "__main__":
    raise SystemExit(main())
