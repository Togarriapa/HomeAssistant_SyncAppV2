"""Validate Supervisor options without logging their contents."""

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import cast

MAX_OPTIONS_BYTES = 65536
_REPO_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class ConfigError(ValueError):
    """Options cannot safely be used."""


@dataclass(frozen=True)
class Config:
    log_level: str = "info"
    status_interval_seconds: int = 300
    repo_b: str | None = None
    github_token: str | None = field(default=None, repr=False)
    repository: str = ""
    github_metadata_token: str = field(default="", repr=False)
    sync_interval_seconds: int = 60
    retrigger_interval_seconds: int = 3600

    @property
    def repository_target(self) -> str:
        return self.repository or self.repo_b or ""

    @property
    def metadata_token(self) -> str:
        return self.github_metadata_token or self.github_token or ""


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
    supported = {f.name for f in fields(Config)}
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
    repository = options.get("repository", "")
    token = options.get("github_metadata_token", "")
    if not isinstance(repository, str) or (repository and not _valid_repo_target(repository)):
        raise ConfigError("Invalid repository target")
    if not isinstance(token, str) or (token and not _valid_token(token)):
        raise ConfigError("Invalid metadata token")
    if repo_b is not None and (repository or token):
        raise ConfigError("Use one repository option pair")
    for name, default, minimum, maximum in (
        ("sync_interval_seconds", 60, 30, 3600),
        ("retrigger_interval_seconds", 3600, 60, 86400),
    ):
        value = options.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ConfigError("Invalid scheduling interval")
    return Config(
        log_level=level,
        status_interval_seconds=interval,
        repo_b=cast(str | None, repo_b),
        github_token=cast(str | None, github_token),
        repository=repository,
        github_metadata_token=token,
        sync_interval_seconds=options.get("sync_interval_seconds", 60),
        retrigger_interval_seconds=options.get("retrigger_interval_seconds", 3600),
    )
