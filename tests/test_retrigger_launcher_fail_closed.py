"""Fail-closed and shutdown contracts for the Retrigger launcher."""

from __future__ import annotations

import signal
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


def test_requested_sigterm_normalizes_only_matching_child_signal_exit() -> None:
    from ha_syncapp.launcher import _normalize_child_return_code

    assert _normalize_child_return_code(-signal.SIGTERM, signal.SIGTERM) == 0
    assert _normalize_child_return_code(0, signal.SIGTERM) == 0
    assert _normalize_child_return_code(7, signal.SIGTERM) == 7
    assert _normalize_child_return_code(-signal.SIGINT, signal.SIGTERM) == -signal.SIGINT


def test_child_exit_is_not_normalized_without_requested_shutdown() -> None:
    from ha_syncapp.launcher import _normalize_child_return_code

    assert _normalize_child_return_code(-signal.SIGTERM, None) == -signal.SIGTERM
    assert _normalize_child_return_code(4, None) == 4
