"""Configuration contract for the always-available recurring Retrigger scheduler."""

from __future__ import annotations

import json

import pytest
from ha_syncapp.config import ConfigError, load_config


def _write_options(tmp_path, options: dict[str, object]) -> None:
    (tmp_path / "options.json").write_text(json.dumps(options), encoding="utf-8")


def test_retrigger_interval_defaults_to_five_minutes(tmp_path) -> None:
    _write_options(tmp_path, {})

    config = load_config(tmp_path / "options.json")

    assert config.retrigger_interval_seconds == 300


@pytest.mark.parametrize("interval", [30, 60, 300, 3600])
def test_retrigger_interval_accepts_bounded_integer(tmp_path, interval: int) -> None:
    _write_options(tmp_path, {"retrigger_interval_seconds": interval})

    config = load_config(tmp_path / "options.json")

    assert config.retrigger_interval_seconds == interval


@pytest.mark.parametrize("interval", [True, False, 0, -1, 29, 3601, 30.0, "300"])
def test_retrigger_interval_rejects_invalid_values(tmp_path, interval: object) -> None:
    _write_options(tmp_path, {"retrigger_interval_seconds": interval})

    with pytest.raises(ConfigError, match="Retrigger interval"):
        load_config(tmp_path / "options.json")
