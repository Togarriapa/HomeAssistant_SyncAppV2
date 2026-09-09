from pathlib import Path

import yaml
from ha_syncapp import __version__
from ha_syncapp.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_packaging_exposes_only_read_only_home_assistant_access() -> None:
    manifest = yaml.safe_load((ROOT / "syncapp/config.yaml").read_text())
    assert set(manifest["arch"]) == {"aarch64", "amd64"}
    assert manifest["version"] == __version__
    assert manifest["stage"] == "experimental"
    assert manifest["boot"] == "manual"
    assert manifest["backup"] == "cold"
    assert manifest["init"] is True
    assert manifest.get("apparmor", True) is True
    assert manifest["homeassistant_api"] is True
    assert manifest["map"] == [
        {
            "type": "homeassistant_config",
            "read_only": True,
            "path": "/homeassistant",
        }
    ]
    for capability in (
        "ports",
        "privileged",
        "full_access",
        "host_network",
        "host_pid",
        "host_ipc",
        "host_dbus",
        "docker_api",
        "hassio_api",
        "auth_api",
        "ingress",
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
    assert manifest["options"].keys() <= manifest["schema"].keys()
    assert "repo_b" not in manifest["options"]
    assert "github_token" not in manifest["options"]
    assert manifest["schema"]["repo_b"].endswith("?")
    assert manifest["schema"]["github_token"].endswith("?")


def test_runtime_dependency_is_exactly_pinned_and_hashed() -> None:
    requirements = (ROOT / "syncapp/requirements.txt").read_text()
    non_comment_lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert non_comment_lines == [
        "websockets==17.1 \\",
        "--hash=sha256:f221081107b8c48184d99f7019604486376e7ef826037e70aad6b02540732c23",
    ]
    assert (ROOT / "requirements-dev.txt").read_text().splitlines()[0] == (
        "-r syncapp/requirements.txt"
    )


def test_container_installs_only_locked_runtime_requirements() -> None:
    dockerfile = (ROOT / "syncapp/Dockerfile").read_text()
    assert "COPY requirements.txt /app/requirements.txt" in dockerfile
    assert (
        "python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt"
        in dockerfile
    )
    assert "pip install websockets" not in dockerfile
