from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from ha_syncapp.__main__ import Shutdown, run
from ha_syncapp.github_repo import RepoIdentity
from ha_syncapp.runtime_event_bridge import RuntimeEventBridgeError

TARGET = "Owner/Home"
GITHUB_TOKEN = "github-secret-sentinel"


class FakeBridge:
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

    def start(self) -> None:
        self._order.append("bridge_start")

    def tick(self) -> None:
        self._order.append("bridge_tick")
        if self._fail_tick:
            raise RuntimeEventBridgeError("secret transport diagnostic")
        self._stop.requested = True

    def stop(self) -> None:
        self._order.append("bridge_stop")


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


def test_configured_service_starts_bridge_after_trust_and_normal_bootstraps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = Shutdown()
    order: list[str] = []
    _trusted_repo(monkeypatch, order)
    bridge = FakeBridge(order, stop)

    def local_bootstrap(store, config, data_dir: Path) -> None:
        assert store.repository_id(TARGET) == 123
        assert config.repo_b == TARGET
        assert data_dir == tmp_path
        order.append("local_bootstrap")

    def runtime_bootstrap(store, config, data_dir: Path) -> None:
        assert store.repository_id(TARGET) == 123
        assert config.repo_b == TARGET
        assert data_dir == tmp_path
        order.append("runtime_bootstrap")

    def build_bridge(store, config, data_dir: Path) -> FakeBridge:
        assert store.repository_id(TARGET) == 123
        assert config.repo_b == TARGET
        assert data_dir == tmp_path
        order.append("bridge_build")
        return bridge

    monkeypatch.setattr("ha_syncapp.__main__._run_startup_local_if_configured", local_bootstrap)
    monkeypatch.setattr("ha_syncapp.__main__._run_startup_runtime_if_configured", runtime_bootstrap)
    monkeypatch.setattr("ha_syncapp.__main__._runtime_event_bridge_if_configured", build_bridge)

    run(tmp_path, stop)
    output = capsys.readouterr().out

    assert order == [
        "trust",
        "local_bootstrap",
        "runtime_bootstrap",
        "bridge_build",
        "bridge_start",
        "bridge_tick",
        "bridge_stop",
    ]
    assert GITHUB_TOKEN not in output
    assert '"mode":"active"' in output


def test_runtime_bridge_failure_stops_transport_and_leaves_run_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = Shutdown()
    order: list[str] = []
    _trusted_repo(monkeypatch, order)
    bridge = FakeBridge(order, stop, fail_tick=True)

    monkeypatch.setattr(
        "ha_syncapp.__main__._run_startup_local_if_configured",
        lambda store, config, data_dir: order.append("local_bootstrap"),
    )
    monkeypatch.setattr(
        "ha_syncapp.__main__._run_startup_runtime_if_configured",
        lambda store, config, data_dir: order.append("runtime_bootstrap"),
    )
    monkeypatch.setattr(
        "ha_syncapp.__main__._runtime_event_bridge_if_configured",
        lambda store, config, data_dir: bridge,
    )

    with pytest.raises(RuntimeEventBridgeError, match="secret transport diagnostic"):
        run(tmp_path, stop)

    output = capsys.readouterr().out
    assert GITHUB_TOKEN not in output
    assert order[-2:] == ["bridge_tick", "bridge_stop"]

    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        active_run_id = database.execute(
            "SELECT active_run_id FROM installation WHERE singleton = 1"
        ).fetchone()[0]
    assert active_run_id is not None


def test_unconfigured_service_remains_passive_and_does_not_start_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "options.json").write_text("{}")
    stop = Shutdown()

    def bootstrap(store, config, data_dir: Path) -> None:
        stop.requested = True

    def forbidden_bridge(*args: object) -> None:
        pytest.fail("unconfigured service attempted runtime event bridge construction")

    monkeypatch.setattr("ha_syncapp.__main__._run_startup_runtime_if_configured", bootstrap)
    monkeypatch.setattr(
        "ha_syncapp.__main__._runtime_event_bridge_if_configured",
        forbidden_bridge,
    )

    run(tmp_path, stop)
    output = capsys.readouterr().out

    assert '"mode":"passive"' in output
