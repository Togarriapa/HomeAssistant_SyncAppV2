from pathlib import Path

import yaml
from ha_syncapp import __version__
from ha_syncapp.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_packaging_limits_access_to_configuration_and_admin_ingress() -> None:
    manifest = yaml.safe_load((ROOT / "syncapp/config.yaml").read_text())
    assert set(manifest["arch"]) == {"aarch64", "amd64"}
    assert manifest["version"] == __version__
    assert manifest["stage"] == "experimental"
    assert manifest["boot"] == "manual"
    assert manifest["backup"] == "cold"
    assert manifest["init"] is True
    assert manifest.get("apparmor", True) is True
    assert manifest["map"] == [
        {"type": "homeassistant_config", "read_only": True, "path": "/homeassistant"}
    ]
    assert manifest["hassio_api"] is True
    assert manifest["homeassistant_api"] is True
    assert manifest["hassio_role"] == "homeassistant"
    assert manifest["ingress"] is True
    assert manifest["panel_admin"] is True
    assert manifest["ingress_port"] == 8099
    for capability in (
        "ports",
        "privileged",
        "full_access",
        "host_network",
        "host_pid",
        "host_ipc",
        "host_dbus",
        "docker_api",
        "auth_api",
        "devices",
        "uart",
        "usb",
        "gpio",
        "journald",
    ):
        assert not manifest.get(capability), f"Unexpected privilege: {capability}"


def test_documented_default_options_are_accepted(tmp_path: Path) -> None:
    import json

    manifest = yaml.safe_load((ROOT / "syncapp/config.yaml").read_text())
    path = tmp_path / "options.json"
    path.write_text(json.dumps(manifest["options"]))
    config = load_config(path)
    assert config.log_level == "info"
    assert config.status_interval_seconds == 300
    assert manifest["schema"].keys() - manifest["options"].keys() == {"repo_b", "github_token"}
