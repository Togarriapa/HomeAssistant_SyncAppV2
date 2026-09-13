"""Launcher integration contract for recurring Retrigger recovery."""

from __future__ import annotations

from pathlib import Path

from ha_syncapp.config import Config
from ha_syncapp.launcher import _build_schedule
from ha_syncapp.retrigger_schedule import RetriggerSchedule


def test_retrigger_schedule_is_not_constructed_without_repo_configuration(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()

    assert _build_schedule(Config(), data) is None


def test_retrigger_schedule_uses_configured_interval_and_optional_recorder(
    monkeypatch, tmp_path: Path
) -> None:
    from ha_syncapp import launcher

    data = tmp_path / "data"
    data.mkdir()
    calls: list[tuple[Path, Path, Path | None]] = []

    def request(socket_path: Path, home: Path, recorder: Path | None) -> None:
        calls.append((socket_path, home, recorder))

    monkeypatch.setattr(launcher, "request_retrigger_once", request)
    config = Config(
        repo_b="owner/repo",
        github_token="token",
        retrigger_interval_seconds=90,
    )

    scheduler = _build_schedule(config, data)

    assert isinstance(scheduler, RetriggerSchedule)
    scheduler.start(10.0)
    assert scheduler.tick(99.999) is None
    assert scheduler.tick(100.0) == "completed"
    assert calls == [(data.resolve() / "syncapp" / "retrigger.sock", Path("/homeassistant"), None)]


def test_retrigger_schedule_passes_explicit_recorder_source(monkeypatch, tmp_path: Path) -> None:
    from ha_syncapp import launcher

    data = tmp_path / "data"
    data.mkdir()
    seen: list[Path | None] = []
    monkeypatch.setattr(
        launcher,
        "request_retrigger_once",
        lambda socket_path, home, recorder: seen.append(recorder),
    )
    config = Config(
        repo_b="owner/repo",
        github_token="token",
        recorder_database_path="/homeassistant/home-assistant_v2.db",
        retrigger_interval_seconds=60,
    )

    scheduler = _build_schedule(config, data)
    assert scheduler is not None
    scheduler.start(0.0)
    assert scheduler.tick(60.0) == "completed"
    assert seen == [Path("/homeassistant/home-assistant_v2.db")]
