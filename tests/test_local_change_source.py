from __future__ import annotations

import os
from pathlib import Path
import pytest

from ha_syncapp.local_change_source import (
    LocalChangeSourceError,
    observe_local_change_source,
)


def test_observation_detects_meaningful_configuration_change(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    configuration = source / "configuration.yaml"
    configuration.write_text("homeassistant:\n", encoding="utf-8")

    before = observe_local_change_source(source)
    configuration.write_text("homeassistant:\n  name: changed\n", encoding="utf-8")
    after = observe_local_change_source(source)

    assert before != after
    assert tuple(entry.path for entry in after.entries) == ("configuration.yaml",)


def test_observation_ignores_recorder_and_home_assistant_log_churn(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n", encoding="utf-8")
    database = source / "home-assistant_v2.db"
    log = source / "home-assistant.log"
    database.write_bytes(b"first")
    log.write_text("first\n", encoding="utf-8")

    before = observe_local_change_source(source)
    database.write_bytes(b"second-generation")
    log.write_text("second generation\n", encoding="utf-8")
    after = observe_local_change_source(source)

    assert before == after
    assert tuple(entry.path for entry in after.entries) == ("configuration.yaml",)


def test_observation_tracks_nested_main_routed_files(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    nested = source / ".storage"
    nested.mkdir(parents=True)
    registry = nested / "core.entity_registry"
    registry.write_text("{}", encoding="utf-8")

    observed = observe_local_change_source(source)

    assert tuple(entry.path for entry in observed.entries) == (".storage/core.entity_registry",)


def test_observation_rejects_source_symlink(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    source = tmp_path / "homeassistant"
    source.symlink_to(actual, target_is_directory=True)

    with pytest.raises(LocalChangeSourceError, match="real directory"):
        observe_local_change_source(source)


def test_observation_rejects_included_symlink(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("secret: outside\n", encoding="utf-8")
    (source / "configuration.yaml").symlink_to(outside)

    with pytest.raises(LocalChangeSourceError, match="symbolic link"):
        observe_local_change_source(source)


def test_observation_rejects_included_hard_link(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("homeassistant:\n", encoding="utf-8")
    os.link(outside, source / "configuration.yaml")

    with pytest.raises(LocalChangeSourceError, match="unsafe file"):
        observe_local_change_source(source)


def test_observation_fails_closed_when_source_disappears(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"

    with pytest.raises(LocalChangeSourceError, match="failed closed"):
        observe_local_change_source(source)
