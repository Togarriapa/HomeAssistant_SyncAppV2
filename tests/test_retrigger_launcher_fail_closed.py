"""The App must not run with configured Repo B but a disabled Retrigger schedule."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from ha_syncapp.config import Config


def test_launcher_stops_child_when_required_schedule_setup_fails(
    monkeypatch, tmp_path: Path
) -> None:
    from ha_syncapp import launcher

    child = Mock()
    child.poll.return_value = None
    child.wait.return_value = 0
    monkeypatch.setattr(launcher.subprocess, "Popen", Mock(return_value=child))
    monkeypatch.setattr(
        launcher,
        "_load_scheduler_config",
        lambda data_dir: Config(repo_b="owner/repo", github_token="token"),
    )
    monkeypatch.setattr(
        launcher,
        "_build_schedule",
        Mock(side_effect=OSError("private implementation detail")),
    )
    monkeypatch.setattr(launcher.signal, "signal", Mock())
    monkeypatch.setattr(launcher.signal, "getsignal", Mock(return_value=object()))

    assert launcher.run([], data_dir=tmp_path) == 14
    child.terminate.assert_called_once_with()
    child.wait.assert_called_once_with()
