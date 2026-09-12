from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp.config import Config
from ha_syncapp.github_repo import RepoIdentity
from ha_syncapp.log_sync_service import LogSyncServiceError
from ha_syncapp.state import StateStore

TARGET = "Owner/Home"
GITHUB_TOKEN = "github-secret-sentinel"
CORE_TOKEN = "supervisor-secret-sentinel"


class FakeRetriggerServer:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> FakeRetriggerServer:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def serve_once(
        self,
        handler: Callable[[object], str],
        *,
        timeout_seconds: float,
    ) -> None:
        del handler
        assert timeout_seconds == 0.25


class FakeLogService:
    def __init__(
        self,
        order: list[str],
        stop: service.Shutdown,
        *,
        fail_tick: bool = False,
    ) -> None:
        self._order = order
        self._stop = stop
        self._fail_tick = fail_tick

    def start(self, now: float) -> None:
        assert now >= 0
        self._order.append("logs_start")

    def tick(self, now: float) -> None:
        assert now >= 0
        self._order.append("logs_tick")
        if self._fail_tick:
            raise LogSyncServiceError("secret logs failure")
        self._stop.requested = True

    def stop(self) -> None:
        self._order.append("logs_stop")


def _write_config(data_dir: Path) -> None:
    (data_dir / "options.json").write_text(
        json.dumps({"repo_b": TARGET, "github_token": GITHUB_TOKEN})
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    def verify(
        target: str,
        token: str,
        *,
        expected_id: int | None = None,
    ) -> RepoIdentity:
        assert target == TARGET
        assert token == GITHUB_TOKEN
        assert expected_id is None
        order.append("trust")
        return RepoIdentity(target=TARGET, repository_id=123)

    monkeypatch.setattr(service, "fetch_and_verify_private_repository", verify)
    monkeypatch.setattr(service, "RetriggerServer", FakeRetriggerServer)
    monkeypatch.setattr(service, "_local_change_service_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_database_sync_service_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_runtime_event_bridge_if_configured", lambda *args: None)


def test_configured_service_runs_periodic_logs_after_trusted_bootstraps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    logs_service = FakeLogService(order, stop)

    monkeypatch.setattr(
        service,
        "_run_startup_local_if_configured",
        lambda *args: order.append("local_bootstrap"),
    )
    monkeypatch.setattr(
        service,
        "_run_startup_database_if_configured",
        lambda *args: order.append("database_bootstrap"),
    )
    monkeypatch.setattr(
        service,
        "_run_startup_runtime_if_configured",
        lambda *args: order.append("runtime_bootstrap"),
    )

    def build_logs(store: StateStore, config: Config, data_dir: Path) -> FakeLogService:
        assert store.repository_id(TARGET) == 123
        assert config.github_token == GITHUB_TOKEN
        assert data_dir == tmp_path
        order.append("logs_build")
        return logs_service

    monkeypatch.setattr(service, "_log_sync_service_if_configured", build_logs)

    service.run(tmp_path, stop)

    assert order == [
        "trust",
        "local_bootstrap",
        "database_bootstrap",
        "runtime_bootstrap",
        "logs_build",
        "logs_start",
        "logs_tick",
        "logs_stop",
    ]
    output = capsys.readouterr().out
    assert GITHUB_TOKEN not in output
    assert '"mode":"active"' in output
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        assert (
            database.execute(
                "SELECT active_run_id FROM installation WHERE singleton = 1"
            ).fetchone()[0]
            is None
        )


def test_shutdown_before_logs_construction_skips_periodic_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    monkeypatch.setattr(service, "_run_startup_local_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_run_startup_database_if_configured", lambda *args: None)

    def runtime_bootstrap(*args: object) -> None:
        order.append("runtime_bootstrap")
        stop.requested = True

    monkeypatch.setattr(service, "_run_startup_runtime_if_configured", runtime_bootstrap)
    monkeypatch.setattr(
        service,
        "_log_sync_service_if_configured",
        lambda *args: pytest.fail("shutdown built the periodic logs service"),
    )

    service.run(tmp_path, stop)

    assert order == ["trust", "runtime_bootstrap"]


def test_periodic_logs_failure_stops_service_and_preserves_interrupted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    monkeypatch.setattr(service, "_run_startup_local_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_run_startup_database_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_run_startup_runtime_if_configured", lambda *args: None)
    monkeypatch.setattr(
        service,
        "_log_sync_service_if_configured",
        lambda *args: FakeLogService(order, stop, fail_tick=True),
    )

    with pytest.raises(LogSyncServiceError, match="secret logs failure"):
        service.run(tmp_path, stop)

    assert order[-3:] == ["logs_start", "logs_tick", "logs_stop"]
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        assert (
            database.execute(
                "SELECT active_run_id FROM installation WHERE singleton = 1"
            ).fetchone()[0]
            is not None
        )


def test_unconfigured_logs_service_is_disabled(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with StateStore(data) as store:
        assert service._log_sync_service_if_configured(store, Config(), data) is None


def test_configured_logs_service_reuses_private_roots_and_hourly_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = Config(repo_b=TARGET, github_token=GITHUB_TOKEN)
    monkeypatch.setenv("SUPERVISOR_TOKEN", CORE_TOKEN)

    with StateStore(data) as store:
        store.bind_repository(TARGET, 123)
        configured = service._log_sync_service_if_configured(store, config, data)

        assert configured is not None
        assert configured._interval_seconds == 60.0 * 60.0
        assert configured._core_token == CORE_TOKEN
        work = data / "syncapp" / "work"
        assert configured._artifact_root == work / "log-artifacts"
        assert configured._snapshot_staging_root == work / "log-snapshots"
        assert configured._workspace_root == work / "log-workspaces"
        for root in (
            configured._artifact_root,
            configured._snapshot_staging_root,
            configured._workspace_root,
        ):
            assert root.is_dir()
            assert root.stat().st_mode & 0o777 == 0o700


def test_configured_logs_service_rejects_symlinked_work_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = Config(repo_b=TARGET, github_token=GITHUB_TOKEN)

    with StateStore(data) as store:
        store.bind_repository(TARGET, 123)
        (data / "syncapp" / "work").symlink_to(outside, target_is_directory=True)
        with pytest.raises(LogSyncServiceError, match="roots are unavailable"):
            service._log_sync_service_if_configured(store, config, data)
