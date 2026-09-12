from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from ha_syncapp.__main__ import Shutdown, run
from ha_syncapp.github_repo import RepoIdentity
from ha_syncapp.local_change_service import LocalChangeServiceError

TARGET = "Owner/Home"
GITHUB_TOKEN = "github-secret-sentinel"


class FakeLocalService:
    def __init__(
        self,
        order: list[str],
        stop: Shutdown,
        *,
        fail_tick: bool = False,
    ) -> None:
        self._order = order
        self._stop = stop
        self._fail_tick = fail_tick

    def start(self, now: float) -> None:
        assert now >= 0
        self._order.append("local_start")

    def tick(self, now: float) -> None:
        assert now >= 0
        self._order.append("local_tick")
        if self._fail_tick:
            raise LocalChangeServiceError("secret local transport diagnostic")
        self._stop.requested = True

    def stop(self) -> None:
        self._order.append("local_stop")


def _write_config(data_dir: Path) -> None:
    (data_dir / "options.json").write_text(
        json.dumps({"repo_b": TARGET, "github_token": GITHUB_TOKEN})
    )


def _trusted_repo(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
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

    monkeypatch.setattr("ha_syncapp.__main__.fetch_and_verify_private_repository", verify)


def test_configured_service_activates_local_events_after_bootstrap_and_stops_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = Shutdown()
    order: list[str] = []
    _trusted_repo(monkeypatch, order)
    local_service = FakeLocalService(order, stop)

    def local_bootstrap(store, config, data_dir: Path) -> None:
        assert store.repository_id(TARGET) == 123
        assert config.repo_b == TARGET
        assert data_dir == tmp_path
        order.append("local_bootstrap")

    def build_local(store, config, data_dir: Path) -> FakeLocalService:
        assert store.repository_id(TARGET) == 123
        assert config.repo_b == TARGET
        assert config.github_token == GITHUB_TOKEN
        assert data_dir == tmp_path
        order.append("local_build")
        return local_service

    def runtime_bootstrap(store, config, data_dir: Path) -> None:
        assert store.repository_id(TARGET) == 123
        order.append("runtime_bootstrap")

    monkeypatch.setattr("ha_syncapp.__main__._run_startup_local_if_configured", local_bootstrap)
    monkeypatch.setattr("ha_syncapp.__main__._local_change_service_if_configured", build_local)
    monkeypatch.setattr("ha_syncapp.__main__._run_startup_runtime_if_configured", runtime_bootstrap)
    monkeypatch.setattr(
        "ha_syncapp.__main__._runtime_event_bridge_if_configured",
        lambda store, config, data_dir: None,
    )

    run(tmp_path, stop)
    output = capsys.readouterr().out

    assert order == [
        "trust",
        "local_bootstrap",
        "local_build",
        "runtime_bootstrap",
        "local_start",
        "local_tick",
        "local_stop",
    ]
    assert GITHUB_TOKEN not in output
    assert '"mode":"active"' in output
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        active_run_id = database.execute(
            "SELECT active_run_id FROM installation WHERE singleton = 1"
        ).fetchone()[0]
    assert active_run_id is None


def test_local_event_failure_stops_transport_and_leaves_run_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = Shutdown()
    order: list[str] = []
    _trusted_repo(monkeypatch, order)
    local_service = FakeLocalService(order, stop, fail_tick=True)

    monkeypatch.setattr(
        "ha_syncapp.__main__._run_startup_local_if_configured",
        lambda store, config, data_dir: order.append("local_bootstrap"),
    )
    monkeypatch.setattr(
        "ha_syncapp.__main__._local_change_service_if_configured",
        lambda store, config, data_dir: local_service,
    )
    monkeypatch.setattr(
        "ha_syncapp.__main__._run_startup_runtime_if_configured",
        lambda store, config, data_dir: order.append("runtime_bootstrap"),
    )
    monkeypatch.setattr(
        "ha_syncapp.__main__._runtime_event_bridge_if_configured",
        lambda store, config, data_dir: None,
    )

    with pytest.raises(LocalChangeServiceError, match="secret local transport diagnostic"):
        run(tmp_path, stop)

    output = capsys.readouterr().out
    assert GITHUB_TOKEN not in output
    assert order[-3:] == ["local_start", "local_tick", "local_stop"]
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        active_run_id = database.execute(
            "SELECT active_run_id FROM installation WHERE singleton = 1"
        ).fetchone()[0]
    assert active_run_id is not None
