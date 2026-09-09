"""Validate Supervisor options without logging their contents."""

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

MAX_OPTIONS_BYTES = 65536


class ConfigError(ValueError):
    """Options cannot safely be used."""


@dataclass(frozen=True)
class Config:
    log_level: str = "info"
    status_interval_seconds: int = 300
    repository: str = ""
    github_metadata_token: str = field(default="", repr=False)
    sync_interval_seconds: int = 60
    retrigger_interval_seconds: int = 3600


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate option keys are not allowed")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ConfigError("Non-finite JSON numbers are not allowed")


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
    if not isinstance(options, dict) or options.keys() - {f.name for f in fields(Config)}:
        raise ConfigError("Options must contain only supported keys")
    level = options.get("log_level", "info")
    if not isinstance(level, str) or level not in ("info", "warning", "error"):
        raise ConfigError("Invalid log level")
    interval = options.get("status_interval_seconds", 300)
    if type(interval) is not int or not 30 <= interval <= 3600:
        raise ConfigError("Status interval must be an integer from 30 to 3600 seconds")
    repository = options.get("repository", "")
    if not isinstance(repository, str) or (
        repository
        and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository)
            or repository.split("/")[1] in (".", "..")
        )
    ):
        raise ConfigError("Repository must be owner/name")
    token = options.get("github_metadata_token", "")
    if not isinstance(token, str) or (token and not re.fullmatch(r"[A-Za-z0-9_]{1,512}", token)):
        raise ConfigError("Invalid metadata token")
    for name, default, minimum, maximum in (
        ("sync_interval_seconds", 60, 30, 3600),
        ("retrigger_interval_seconds", 3600, 60, 86400),
    ):
        value = options.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ConfigError("Invalid scheduling interval")
    return Config(**options)
