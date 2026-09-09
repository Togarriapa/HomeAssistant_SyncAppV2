"""Validate Supervisor options without logging their contents."""

import json
from dataclasses import dataclass
from pathlib import Path

MAX_OPTIONS_BYTES = 65536


class ConfigError(ValueError):
    """Options cannot safely be used."""


@dataclass(frozen=True)
class Config:
    log_level: str = "info"
    status_interval_seconds: int = 300


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
    if not isinstance(options, dict) or options.keys() - {"log_level", "status_interval_seconds"}:
        raise ConfigError("Options must contain only supported keys")
    level = options.get("log_level", "info")
    if not isinstance(level, str) or level not in ("info", "warning", "error"):
        raise ConfigError("Invalid log level")
    interval = options.get("status_interval_seconds", 300)
    if type(interval) is not int or not 30 <= interval <= 3600:
        raise ConfigError("Status interval must be an integer from 30 to 3600 seconds")
    return Config(log_level=level, status_interval_seconds=interval)
