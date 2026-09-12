from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp.config import Config
from ha_syncapp.database_sync_service import DatabaseSyncServiceError
from ha_syncapp.github_repo import RepoIdentity
from ha_syncapp.state import StateStore

TARGET = "Owner/Home"
TOKEN = "github-secret-sentinel"


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


class FakeDatabaseService:
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
        self._order.append("database_start")

    def tick(self, now: float) -> None:
        assert now >= 0
        self._order.append("database_tick")
        if self._fail_tick:
            raise DatabaseSyncServiceError("secret database failure")
        self._stop.requested = True

    def stop(self) -> None:
        self._order.append("database_stop")


def _write_config(data_dir: Path) -> None:
    (data_dir / "options.json").write_text(
        json.dumps(
            {
                "repo_b": TARGET,
                "github_token": TOKEN,
                "recorder_database_path": "/homeassistant/recorder.db",
            }
        )
    )


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    order: list[str],
) -> None:
    def verify(
        target: str,
        token: str,
        *,
        expected_id: int | None = None,
    ) -> RepoIdentity:
        assert target == TARGET
        assert token == TOKEN
        assert expected_id is None
        order.append("trust")
        return RepoIdentity(target=TARGET, repository_id=123)

    monkeypatch.setattr(service, "fetch_and_verify_private_repository", verify)
    monkeypatch.setattr(service, "RetriggerServer", FakeRetriggerServer)
    monkeypatch.setattr(
        service,
        "_run_startup_local_if_configured",
        lambda *args: order.append("local_bootstrap"),
    )
    monkeypatch.setattr(service, "_local_change_service_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_runtime_event_bridge_if_configured", lambda *args: None)


def test_configured_service_runs_periodic_database_after_trusted_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    database_service = FakeDatabaseService(order, stop)

    def database_bootstrap(store: StateStore, config: Config, data_dir: Path) -> None:
        assert store.repository_id(TARGET) == 123
        assert config.recorder_database_path == "/homeassistant/recorder.db"
        assert data_dir == tmp_path
        order.append("database_bootstrap")

    def build_database(
        store: StateStore,
        config: Config,
        data_dir: Path,
    ) -> FakeDatabaseService:
        assert store.repository_id(TARGET) == 123
        assert config.github_token == TOKEN
        assert data_dir == tmp_path
        order.append("database_build")
        return database_service

    monkeypatch.setattr(service, "_run_startup_database_if_configured", database_bootstrap)
    monkeypatch.setattr(service, "_database_sync_service_if_configured", build_database)
    monkeypatch.setattr(
        service,
        "_run_startup_runtime_if_configured",
        lambda *args: order.append("runtime_bootstrap"),
    )

    service.run(tmp_path, stop)

    assert order == [
        "trust",
        "local_bootstrap",
        "database_bootstrap",
        "database_build",
        "runtime_bootstrap",
        "database_start",
        "database_tick",
        "database_stop",
    ]
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert '"mode":"active"' in output
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        assert (
            database.execute(
                "SELECT active_run_id FROM installation WHERE singleton = 1"
            ).fetchone()[0]
            is None
        )


def test_shutdown_after_database_bootstrap_never_builds_periodic_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)

    def database_bootstrap(*args: object) -> None:
        order.append("database_bootstrap")
        stop.requested = True

    monkeypatch.setattr(service, "_run_startup_database_if_configured", database_bootstrap)
    monkeypatch.setattr(
        service,
        "_database_sync_service_if_configured",
        lambda *args: pytest.fail("shutdown built the periodic database service"),
    )
    monkeypatch.setattr(
        service,
        "_run_startup_runtime_if_configured",
        lambda *args: pytest.fail("shutdown ran runtime bootstrap"),
    )

    service.run(tmp_path, stop)

    assert order == ["trust", "local_bootstrap", "database_bootstrap"]


def test_periodic_database_failure_stops_service_and_preserves_interrupted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    database_service = FakeDatabaseService(order, stop, fail_tick=True)
    monkeypatch.setattr(
        service,
        "_run_startup_database_if_configured",
        lambda *args: order.append("database_bootstrap"),
    )
    monkeypatch.setattr(
        service,
        "_database_sync_service_if_configured",
        lambda *args: database_service,
    )
    monkeypatch.setattr(service, "_run_startup_runtime_if_configured", lambda *args: None)

    with pytest.raises(DatabaseSyncServiceError, match="secret database failure"):
        service.run(tmp_path, stop)

    assert order[-3:] == ["database_start", "database_tick", "database_stop"]
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        assert (
            database.execute(
                "SELECT active_run_id FROM installation WHERE singleton = 1"
            ).fetchone()[0]
            is not None
        )


def test_unconfigured_database_service_is_disabled(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with StateStore(data) as store:
        assert (
            service._database_sync_service_if_configured(
                store,
                Config(repo_b=TARGET, github_token=TOKEN),
                data,
                tmp_path / "homeassistant",
            )
            is None
        )


def test_configured_database_service_reuses_private_roots_and_hourly_cadence(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    home = tmp_path / "homeassistant"
    home.mkdir()
    source = home / "recorder.db"
    source.write_bytes(b"sqlite-placeholder")
    config = Config(
        repo_b=TARGET,
        github_token=TOKEN,
        recorder_database_path=str(source),
    )

    with StateStore(data) as store:
        store.bind_repository(TARGET, 123)
        configured = service._database_sync_service_if_configured(store, config, data, home)

        assert configured is not None
        assert configured._source_database == source
        assert configured._interval_seconds == 60.0 * 60.0
        work = data / "syncapp" / "work"
        assert configured._database_staging_root == work / "database-staging"
        assert configured._snapshot_staging_root == work / "database-snapshots"
        assert configured._workspace_root == work / "database-workspaces"


def test_configured_database_service_rejects_parent_symlink_escape(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    home = tmp_path / "homeassistant"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "recorder.db"
    source.write_bytes(b"sqlite-placeholder")
    (home / "linked").symlink_to(outside, target_is_directory=True)
    config = Config(
        repo_b=TARGET,
        github_token=TOKEN,
        recorder_database_path=str(home / "linked" / "recorder.db"),
    )

    with StateStore(data) as store:
        store.bind_repository(TARGET, 123)
        with pytest.raises(DatabaseSyncServiceError, match="escapes Home Assistant"):
            service._database_sync_service_if_configured(store, config, data, home)
