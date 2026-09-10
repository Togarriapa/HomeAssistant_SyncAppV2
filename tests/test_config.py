import json
from pathlib import Path

import pytest
from ha_syncapp.config import Config, ConfigError, load_config


def write_options(tmp_path: Path, value: object) -> Path:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(value))
    return path


def test_defaults_are_passive(tmp_path: Path) -> None:
    assert load_config(write_options(tmp_path, {})) == Config(
        log_level="info",
        status_interval_seconds=300,
        repo_b=None,
        github_token=None,
        recorder_database_path=None,
    )


def test_explicit_options(tmp_path: Path) -> None:
    config = load_config(
        write_options(
            tmp_path,
            {
                "log_level": "warning",
                "status_interval_seconds": 30,
                "repo_b": "Owner/Home",
                "github_token": "secret-sentinel",
                "recorder_database_path": "/homeassistant/recorder.db",
            },
        )
    )
    assert config.log_level == "warning"
    assert config.status_interval_seconds == 30
    assert config.repo_b == "Owner/Home"
    assert config.github_token == "secret-sentinel"
    assert config.recorder_database_path == "/homeassistant/recorder.db"


@pytest.mark.parametrize(
    "value",
    [
        [],
        None,
        "secret-sentinel",
        {"secret-sentinel": "secret-sentinel"},
        {"deployment_enabled": True},
        {"log_level": "secret-sentinel"},
        {"log_level": []},
        {"status_interval_seconds": True},
        {"status_interval_seconds": "300"},
        {"status_interval_seconds": 30.0},
        {"status_interval_seconds": 29},
        {"status_interval_seconds": 3601},
        {"repo_b": "Owner/Home"},
        {"github_token": "secret-sentinel"},
        {"repo_b": "https://github.com/Owner/Home", "github_token": "secret-sentinel"},
        {"repo_b": "Owner", "github_token": "secret-sentinel"},
        {"repo_b": "Owner/Home/Extra", "github_token": "secret-sentinel"},
        {"repo_b": "Owner Home/Repo", "github_token": "secret-sentinel"},
        {"repo_b": "Owner/Home", "github_token": ""},
        {"repo_b": "Owner/Home", "github_token": " secret-sentinel"},
        {"repo_b": "Owner/Home", "github_token": "secret\nsentinel"},
        {"recorder_database_path": "homeassistant/recorder.db"},
        {"recorder_database_path": "/homeassistant"},
        {"recorder_database_path": "/homeassistant/../etc/passwd"},
        {"recorder_database_path": "/homeassistant//recorder.db"},
        {"recorder_database_path": "/homeassistant/recorder.db/"},
        {"recorder_database_path": "/tmp/recorder.db"},
        {"recorder_database_path": "/homeassistant/secret-sentinel\n.db"},
        {"recorder_database_path": "/homeassistant/secret-sentinel\x7f.db"},
        {"recorder_database_path": 42},
    ],
)
def test_invalid_options_fail_without_disclosing_input(tmp_path: Path, value: object) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(write_options(tmp_path, value))
    assert "secret-sentinel" not in str(error.value)


def test_recorder_database_path_may_select_nested_config_file(tmp_path: Path) -> None:
    config = load_config(
        write_options(
            tmp_path,
            {"recorder_database_path": "/homeassistant/storage/recorder.db"},
        )
    )
    assert config.recorder_database_path == "/homeassistant/storage/recorder.db"


@pytest.mark.parametrize(
    "contents",
    [
        b'{"secret-sentinel":',
        b"\xff",
        b'{"log_level":"info","log_level":"error"}',
        b'{"status_interval_seconds":NaN}',
        b" " * 65537,
    ],
)
def test_malformed_or_ambiguous_options_are_rejected(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / "options.json"
    path.write_bytes(contents)
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_options_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.json")
