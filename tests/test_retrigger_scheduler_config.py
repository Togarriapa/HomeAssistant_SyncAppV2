from __future__ import annotations

import json
from pathlib import Path

import pytest
from ha_syncapp.config import ConfigError, load_config


def _load(tmp_path: Path, options: dict[str, object]) -> object:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options), encoding="utf-8")
    return load_config(path)


def test_retrigger_interval_defaults_to_five_minutes(tmp_path: Path) -> None:
    config = _load(tmp_path, {})
    assert config.retrigger_interval_seconds == 300


@pytest.mark.parametrize("value", [30, 300, 3600])
def test_retrigger_interval_accepts_bounded_integer(tmp_path: Path, value: int) -> None:
    config = _load(tmp_path, {"retrigger_interval_seconds": value})
    assert config.retrigger_interval_seconds == value


@pytest.mark.parametrize("value", [29, 3601, 0, -1, True, 30.5, "300", None])
def test_retrigger_interval_rejects_disable_sentinels_and_invalid_types(
    tmp_path: Path, value: object
) -> None:
    with pytest.raises(ConfigError, match="Retrigger interval"):
        _load(tmp_path, {"retrigger_interval_seconds": value})
