"""Explicit path routing for the Repo B main configuration branch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_RECORDER_DATABASE = "home-assistant_v2.db"
_HOME_ASSISTANT_LOG = "home-assistant.log"


@dataclass(frozen=True, slots=True)
class MainPathRouter:
    """Validated callable routing one configured Home Assistant source."""

    recorder_relative_path: str | None = None

    def __call__(self, relative_path: str) -> bool:
        return include_in_main(
            relative_path,
            recorder_relative_path=self.recorder_relative_path,
        )


def build_main_path_router(
    source_root: Path,
    recorder_database: Path | None = None,
) -> MainPathRouter:
    """Bind Repo B main routing to one explicit Recorder source path."""
    if not isinstance(source_root, Path) or not source_root.is_absolute():
        raise ValueError("Home Assistant source path is invalid")
    if recorder_database is None:
        return MainPathRouter()
    if not isinstance(recorder_database, Path) or not recorder_database.is_absolute():
        raise ValueError("configured Recorder path is invalid")
    try:
        relative = recorder_database.relative_to(source_root)
    except ValueError:
        raise ValueError("configured Recorder path is invalid") from None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("configured Recorder path is invalid")
    return MainPathRouter(relative.as_posix())


def include_in_main(
    relative_path: str,
    *,
    recorder_relative_path: str | None = None,
) -> bool:
    """Return whether one Home Assistant source file belongs in Repo B main."""
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("main branch path is invalid")
    if recorder_relative_path is not None and (
        relative_path == recorder_relative_path
        or relative_path.startswith(f"{recorder_relative_path}-")
    ):
        return False
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
