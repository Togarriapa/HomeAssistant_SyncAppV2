"""Explicit path routing for the Repo B main configuration branch."""

from __future__ import annotations

_RECORDER_DATABASE = "home-assistant_v2.db"
_HOME_ASSISTANT_LOG = "home-assistant.log"


def include_in_main(relative_path: str) -> bool:
    """Return whether one Home Assistant source file belongs in Repo B main."""
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("main branch path is invalid")
    if "/" not in relative_path:
        if relative_path == _RECORDER_DATABASE or relative_path.startswith(
            f"{_RECORDER_DATABASE}-"
        ):
            return False
        if relative_path == _HOME_ASSISTANT_LOG or relative_path.startswith(
            f"{_HOME_ASSISTANT_LOG}."
        ):
            return False
    return True
