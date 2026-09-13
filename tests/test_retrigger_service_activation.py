"""Service integration contract for recurring Retrigger recovery."""

from __future__ import annotations

from pathlib import Path

from ha_syncapp.config import Config
from ha_syncapp.retrigger_schedule import RetriggerSchedule


def test_retrigger_schedule_is_not_constructed_without_trusted_repo(monkeypatch, tmp_path: Path) -> None:
    from ha_syncapp import __main__ as service

    config = Config()

    assert service._retrigger_schedule_if_configured(object(), config, tmp_path) is None  # type: ignore[arg-type]


def test_retrigger_schedule_uses_configured_interval(monkeypatch, tmp_path: Path) -> None:
    from ha_syncapp import __main__ as service

    class Store:
        def repository_id(self, target: str) -> int | None:
            return 123 if target == "owner/repo" else None

    monkeypatch.setattr(service, "_scheduled_retrigger_once", lambda *args, **kwargs: "completed")
    config = Config(
        repo_b="owner/repo",
        github_token="token",
        recorder_database_path="/homeassistant/home-assistant_v2.db",
        retrigger_interval_seconds=90,
    )

    scheduler = service._retrigger_schedule_if_configured(Store(), config, tmp_path)  # type: ignore[arg-type]

    assert isinstance(scheduler, RetriggerSchedule)
    scheduler.start(10.0)
    assert scheduler.tick(99.999) is None
    assert scheduler.tick(100.0) == "completed"
