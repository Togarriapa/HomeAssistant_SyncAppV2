from pathlib import Path

import yaml
from ha_syncapp import __version__
from ha_syncapp.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_packaging_exposes_only_required_read_only_home_assistant_access() -> None:
    manifest = yaml.safe_load((ROOT / "syncapp/config.yaml").read_text())
    assert set(manifest["arch"]) == {"aarch64", "amd64"}
    assert manifest["version"] == __version__
    assert manifest["stage"] == "experimental"
    assert manifest["boot"] == "manual"
    assert manifest["backup"] == "cold"
    assert manifest["init"] is True
    assert manifest.get("apparmor", True) is True
    assert manifest["homeassistant_api"] is True
    assert manifest["hassio_api"] is True
    assert manifest["hassio_role"] == "backup"
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
        "auth_api",
        "ingress",
        "devices",
        "uart",
        "usb",
        "gpio",
        "journald",
    ):
        assert not manifest.get(capability), f"Unexpected privilege: {capability}"


def test_semantic_validator_has_mandatory_app_armor_child_transition() -> None:
    profile = (ROOT / "syncapp/apparmor.txt").read_text()
    dockerfile = (ROOT / "syncapp/Dockerfile").read_text()
    semantics = (ROOT / "syncapp/src/ha_syncapp/candidate_semantics.py").read_text()
    child = (ROOT / "syncapp/src/ha_syncapp/_validator_child.py").read_text()

    assert "profile homeassistant_syncapp_v2 " in profile
    assert "/opt/syncapp-validator/python3 cx -> validator," in profile
    assert "profile validator flags=" in profile
    assert "complain" not in profile.casefold()
    child_profile = profile.split("profile validator", 1)[1]
    child_rules = {
        line.strip()
        for line in child_profile.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "/proc/*/attr/current r," in child_rules
    assert "/proc/**" not in child_rules
    assert not any(rule.startswith("/data/") for rule in child_rules)
    assert not any(rule.startswith("/homeassistant/") for rule in child_rules)
    assert "/usr/src/homeassistant/homeassistant/** r," in child_rules
    assert "/usr/src/** r," not in child_rules
    assert "network," not in child_rules
    assert "install -m 0555 /usr/local/bin/python3 /opt/syncapp-validator/python3" in dockerfile
    assert '_VALIDATOR_PYTHON = "/opt/syncapp-validator/python3"' in semantics
    assert "homeassistant_syncapp_v2//validator" in child
    assert "_EXPECTED_APPARMOR_PROFILE.fullmatch" in child


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


def test_runtime_dependencies_are_exactly_pinned_and_hashed() -> None:
    requirements = (ROOT / "syncapp/requirements.txt").read_text()
    non_comment_lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert non_comment_lines == [
        "websockets==17.1 \\",
        "--hash=sha256:f221081107b8c48184d99f7019604486376e7ef826037e70aad6b02540732c23",
        "PyYAML==6.0.3 \\",
        "--hash=sha256:ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc \\",
        "--hash=sha256:9149cad251584d5fb4981be1ecde53a1ca46c891a79788c0df828d2f166bda28 \\",
        "--hash=sha256:7c6610def4f163542a622a73fb39f534f8c101d690126992300bf3207eab9764 \\",
        "--hash=sha256:5190d403f121660ce8d1d2c1bb2ef1bd05b5f68533fc5c2ea899bd15f4399b35",
    ]
    assert "websockets" not in (ROOT / "requirements-dev.txt").read_text()


def test_quality_ci_installs_runtime_hashes_separately() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "python -m pip install --require-hashes -r syncapp/requirements.txt" in workflow
    assert "python -m pip install -r requirements-dev.txt" in workflow


def test_container_installs_only_locked_runtime_requirements() -> None:
    dockerfile = (ROOT / "syncapp/Dockerfile").read_text()
    assert "COPY requirements.txt /app/requirements.txt" in dockerfile
    assert (
        "python -m pip install --no-cache-dir --require-hashes -r /app/requirements.txt"
        in dockerfile
    )
    assert "pip install websockets" not in dockerfile
