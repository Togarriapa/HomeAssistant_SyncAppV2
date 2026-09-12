"""Validate Supervisor options without logging their contents."""

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import cast

MAX_OPTIONS_BYTES = 65536
_REPO_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_HOMEASSISTANT_ROOT = PurePosixPath("/homeassistant")
_MAX_PATH_LENGTH = 4096


class ConfigError(ValueError):
    """Options cannot safely be used."""


@dataclass(frozen=True)
class Config:
    log_level: str = "info"
    status_interval_seconds: int = 300
    repo_b: str | None = None
    github_token: str | None = field(default=None, repr=False)
    recorder_database_path: str | None = None
    recorder_retention_days: int = 7


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate option keys are not allowed")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ConfigError("Non-finite JSON numbers are not allowed")


def _valid_repo_target(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    if len(parts) != 2:
        return False
    owner, repository = parts
    return bool(
        _REPO_OWNER.fullmatch(owner)
        and _REPO_NAME.fullmatch(repository)
        and repository not in {".", ".."}
    )


def _valid_token(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 512
        and value == value.strip()
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _valid_recorder_database_path(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_PATH_LENGTH:
        return False
    if value != value.strip() or any(ord(character) < 0x20 for character in value):
        return False
    if "\x7f" in value or not value.startswith("/") or posixpath.normpath(value) != value:
        return False
    path = PurePosixPath(value)
    return path != _HOMEASSISTANT_ROOT and path.is_relative_to(_HOMEASSISTANT_ROOT)


def load_config(path: Path) -> Config:
    """Read a bounded JSON object; reject unknown options and coercion."""
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_OPTIONS_BYTES + 1)
        if len(raw) > MAX_OPTIONS_BYTES:
            raise ConfigError("Options exceed the size limit")
        options = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ConfigError("Unable to read valid options") from None
    supported = {
        "log_level",
        "status_interval_seconds",
        "repo_b",
        "github_token",
        "recorder_database_path",
        "recorder_retention_days",
    }
    if not isinstance(options, dict) or options.keys() - supported:
        raise ConfigError("Options must contain only supported keys")
    level = options.get("log_level", "info")
    if not isinstance(level, str) or level not in ("info", "warning", "error"):
        raise ConfigError("Invalid log level")
    interval = options.get("status_interval_seconds", 300)
    if type(interval) is not int or not 30 <= interval <= 3600:
        raise ConfigError("Status interval must be an integer from 30 to 3600 seconds")

    repo_b = options.get("repo_b")
    github_token = options.get("github_token")
    if (repo_b is None) != (github_token is None):
        raise ConfigError("Repo B and GitHub authentication must be configured together")
    if repo_b is not None and not _valid_repo_target(repo_b):
        raise ConfigError("Invalid Repo B target")
    if github_token is not None and not _valid_token(github_token):
        raise ConfigError("Invalid GitHub authentication")

    recorder_database_path = options.get("recorder_database_path")
    if recorder_database_path is not None and not _valid_recorder_database_path(
        recorder_database_path
    ):
        raise ConfigError("Invalid Recorder database path")

    recorder_retention_days = options.get("recorder_retention_days", 7)
    if type(recorder_retention_days) is not int or not 1 <= recorder_retention_days <= 365:
        raise ConfigError("Recorder retention must be an integer from 1 to 365 days")

    return Config(
        log_level=level,
        status_interval_seconds=interval,
        repo_b=cast(str | None, repo_b),
        github_token=cast(str | None, github_token),
        recorder_database_path=cast(str | None, recorder_database_path),
        recorder_retention_days=recorder_retention_days,
    )
