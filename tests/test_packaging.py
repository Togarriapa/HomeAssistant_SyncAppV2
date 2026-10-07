import hashlib
from pathlib import Path

import yaml
from ha_syncapp import __version__
from ha_syncapp.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CORE_VERSION = "2026.9.4"
CORE_IMAGE_INDEX = "3e6710a7ab2a61311d9d899b719f6c3657791c63e8f4942cec4ebc42401d6b76"


def test_core_validator_base_is_versioned_and_digest_pinned() -> None:
    dockerfile = (ROOT / "syncapp/Dockerfile").read_text()
    bases = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert bases == [
        f"FROM ghcr.io/home-assistant/home-assistant:{CORE_VERSION}@sha256:{CORE_IMAGE_INDEX}"
    ]


def test_core_validator_version_bindings_and_documentation_are_consistent() -> None:
    semantics = (ROOT / "syncapp/src/ha_syncapp/candidate_semantics.py").read_text()
    fixture_probe = (ROOT / "scripts/core_fixture_probe.py").read_text()
    validation_docs = (ROOT / "docs/candidate-validation.md").read_text()
    development_docs = (ROOT / "docs/development.md").read_text()

    assert f'BUNDLED_CORE_VERSION = "{CORE_VERSION}"' in semantics
    assert f'Path(sys.argv[1]), "{CORE_VERSION}"' in fixture_probe
    assert f"Core **{CORE_VERSION}** runtime" in validation_docs
    assert f"core/blob/{CORE_VERSION}/homeassistant/scripts/check_config.py" in validation_docs
    assert f"Core `{CORE_VERSION}` tag" in development_docs
    assert CORE_IMAGE_INDEX in development_docs
    assert "e47c978e1b801466e7f62f612fd552bc3a228e077b31a3f1c22c05cf63d754da" in development_docs
    assert "35e6df56a9ce632c9b15df869ac73a17af6cdd2cfb99830527ffac9cc5218ba2" in development_docs


def test_packaging_exposes_only_required_read_only_home_assistant_access() -> None:
    manifest = yaml.safe_load((ROOT / "syncapp/config.yaml").read_text())
    assert set(manifest["arch"]) == {"aarch64", "amd64"}
    assert manifest["version"] == __version__
    assert manifest["stage"] == "experimental"
    assert manifest["boot"] == "auto"
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
    assert config.retrigger_interval_seconds == 300
    assert config.recorder_retention_days == 7
    assert manifest["options"].keys() <= manifest["schema"].keys()
    assert "repo_b" not in manifest["options"]
    assert "github_token" not in manifest["options"]
    assert manifest["schema"]["repo_b"].endswith("?")
    assert manifest["schema"]["github_token"].endswith("?")
    assert manifest["schema"]["retrigger_interval_seconds"] == "int(30,3600)"
    assert manifest["schema"]["recorder_retention_days"] == "int(1,365)"
    assert "administrative_retry_request_id" not in manifest["options"]
    assert "administrative_retry_work_kind" not in manifest["options"]
    assert "administrative_retry_work_key" not in manifest["options"]
    assert manifest["schema"]["administrative_retry_request_id"] == "str?"
    assert manifest["schema"]["administrative_retry_work_kind"] == "str?"
    assert manifest["schema"]["administrative_retry_work_key"] == "password?"
    assert "repo_b_admin_action" not in manifest["options"]
    assert "repo_b_admin_request_id" not in manifest["options"]
    assert manifest["schema"]["repo_b_admin_action"] == (
        "list(generate|test|rotate_prepare|rotate_verify|rotate_activate|initialize)?"
    )
    assert manifest["schema"]["repo_b_admin_request_id"] == "str?"

    translations = yaml.safe_load((ROOT / "syncapp/translations/en.yaml").read_text())
    assert "retrigger_interval_seconds" in translations["configuration"]
    assert "recorder_retention_days" in translations["configuration"]
    assert "administrative_retry_request_id" in translations["configuration"]
    assert "administrative_retry_work_kind" in translations["configuration"]
    assert "administrative_retry_work_key" in translations["configuration"]
    assert "repo_b_admin_action" in translations["configuration"]
    assert "repo_b_admin_request_id" in translations["configuration"]
    operator_control_docs = (ROOT / "docs/deploy-key-operator-controls.md").read_text()
    assert "generate → manually enroll → test" in operator_control_docs
    assert (
        "rotate_prepare → manually enroll → rotate_verify → rotate_activate"
        in operator_control_docs
    )
    assert "never enrolls or removes a GitHub deploy key" in operator_control_docs
    assert "does not bypass validation, backup, observation, or rollback" in operator_control_docs

    operator_docs = (ROOT / "syncapp/DOCS.md").read_text()
    assert "`repo_b_admin_action`" in operator_docs
    assert "Generate a new UUIDv4 for every new action" in operator_docs
    assert "`retrigger_interval_seconds` | `300` | Integer from 30 through 3600" in operator_docs
    assert "`recorder_retention_days` | `7` | Integer from 1 through 365" in operator_docs
    assert "`administrative_retry_request_id`" in operator_docs
    assert "A request UUID is consumed exactly once" in operator_docs
    assert "## Repo B deploy-key foundation" in operator_docs
    deploy_key_docs = (ROOT / "docs/deploy-key-generation.md").read_text()
    assert "does not enroll, rotate, or use the key" in deploy_key_docs
    assert "never returned or logged" in deploy_key_docs
    assert "Generation performs no network operation" in deploy_key_docs
    access_docs = (ROOT / "docs/deploy-key-access-test.md").read_text()
    assert "Exactly one bounded, noninteractive `git ls-remote --refs`" in access_docs
    assert "Strict host-key checking is mandatory" in access_docs
    assert "proof grants no mutation or deployment authority" in access_docs
    rotation_docs = (ROOT / "docs/deploy-key-rotation.md").read_text()
    assert "Retrigger and periodic services never start or advance it" in rotation_docs
    assert "does **not** establish write permission" in rotation_docs
    assert "previous generation is retained" in rotation_docs
    candidate_fetch_docs = (ROOT / "docs/deploy-key-candidate-fetch.md").read_text()
    assert "No GitHub token" in candidate_fetch_docs
    assert "must never silently return to token-backed Git transport" in candidate_fetch_docs
    publication_docs = (ROOT / "docs/deploy-key-publication-transport.md").read_text()
    assert "No `--force`" in publication_docs
    assert "only through an inherited descriptor" in publication_docs
    assert "must never silently fall back to token-backed Git transport" in publication_docs
    promotion_docs = (ROOT / "docs/deploy-key-promotion-transport.md").read_text()
    assert "one `git push --atomic` transaction" in promotion_docs
    assert "Cleanup is restricted to a resolved child" in promotion_docs
    assert "must never silently fall back to a token-backed Git transport" in promotion_docs


def test_runtime_dependencies_are_exactly_pinned_and_hashed() -> None:
    requirements = (ROOT / "syncapp/requirements.txt").read_text()
    non_comment_lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    pyyaml_index = non_comment_lines.index("PyYAML==6.0.3 \\")
    websocket_lines = non_comment_lines[:pyyaml_index]
    websocket_hashes = websocket_lines[1:]

    assert websocket_lines[0] == "websockets==17.2 \\"
    assert len(websocket_hashes) == 148
    assert len(set(websocket_hashes)) == 148
    assert all(
        line.startswith("--hash=sha256:")
        and len(line.removesuffix(" \\")) == len("--hash=sha256:") + 64
        for line in websocket_hashes
    )
    assert all(line.endswith(" \\") for line in websocket_hashes[:-1])
    assert not websocket_hashes[-1].endswith(" \\")
    websocket_lock_fingerprint = hashlib.sha256("\n".join(websocket_hashes).encode()).hexdigest()
    assert (
        websocket_lock_fingerprint
        == "933170b725d0c410ff15631bc70f9a9ad3f39661b692c5b7e0307c348831b4de"
    )
    assert non_comment_lines[pyyaml_index:] == [
        "PyYAML==6.0.3 \\",
        "--hash=sha256:ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc \\",
        "--hash=sha256:9149cad251584d5fb4981be1ecde53a1ca46c891a79788c0df828d2f166bda28 \\",
        "--hash=sha256:7c6610def4f163542a622a73fb39f534f8c101d690126992300bf3207eab9764 \\",
        "--hash=sha256:5190d403f121660ce8d1d2c1bb2ef1bd05b5f68533fc5c2ea899bd15f4399b35",
    ]
    assert "websockets" not in (ROOT / "requirements-dev.txt").read_text()


def test_consolidated_deploy_key_operator_runbook_contract() -> None:
    operator_docs = (ROOT / "syncapp/DOCS.md").read_text()
    runbook_path = ROOT / "docs/repo-b-deploy-key-operations.md"
    assert runbook_path.is_file()
    runbook = runbook_path.read_text()

    assert "[Repo B deploy-key operations]" in operator_docs
    for option in (
        "repo_b_candidate_transport",
        "repo_b_publication_transport",
        "repo_b_promotion_transport",
        "repo_b_rollback_transport",
        "repo_b_retention_transport",
    ):
        assert f"`{option}`" in runbook

    required_sections = (
        "## Prerequisites and authority boundaries",
        "## First enrollment and initialization",
        "## Staged transport rollout",
        "## Verification",
        "## Rotation",
        "## Failure and recovery matrix",
        "## Compatibility rollback",
        "## Key removal",
    )
    for section in required_sections:
        assert section in runbook

    normalized = " ".join(runbook.split())
    for boundary in (
        "GitHub REST identity verification",
        "Settings → Deploy keys",
        "Allow write access",
        "never delete the only working credential",
        "does not bypass candidate validation",
        "does not bypass backup",
        "does not bypass deployment observation",
        "does not bypass rollback",
        "Retrigger remains enabled",
        "explicit administrative retry",
    ):
        assert boundary in normalized

    for specialist in (
        "deploy-key-generation.md",
        "deploy-key-access-test.md",
        "deploy-key-rotation.md",
        "repo-b-initialization-execution.md",
        "deploy-key-candidate-ingress-adoption.md",
        "deploy-key-snapshot-publication-adoption.md",
        "deploy-key-promotion-adoption.md",
        "deploy-key-rollback-adoption.md",
        "deploy-key-retention-adoption.md",
    ):
        assert specialist in runbook


def test_current_architecture_documentation_contract() -> None:
    architecture = (ROOT / "docs/architecture.md").read_text()

    for required in (
        "# Current architecture",
        "schema 38",
        "Routine producers",
        "Retrigger recovery",
        "Candidate deployment",
        "Deploy-key administration and transport",
        "Runtime and deployment observability",
        "Physical HAOS release gates",
        "haos-release-evidence.md",
        "issues/212",
        "issues/251",
    ):
        assert required in architecture

    for stale_claim in (
        "performs no synchronization or deployment",
        "does not implement the scheduler itself",
        "High-value remaining prerequisites include",
    ):
        assert stale_claim not in architecture

    for specialist in (
        "apply-authorization.md",
        "database-history-replacement.md",
        "routine-work-scheduling.md",
    ):
        content = (ROOT / f"docs/{specialist}").read_text()
        assert "## Current integration status" in content


def test_specialist_runtime_documents_describe_live_service_integration() -> None:
    local_cycle = (ROOT / "docs/local-sync-cycle.md").read_text()
    routine = (ROOT / "docs/routine-work-scheduling.md").read_text()
    normalized_routine = " ".join(routine.split())

    for required in (
        "## Current integration status",
        "`LocalChangeService`",
        "`run_local_sync_process()`",
        "`run_local_sync_retrigger_pass()`",
    ):
        assert required in local_cycle

    for stale_claim in (
        "This coordinator is not yet invoked by the long-running service.",
        "Event-driven change detection, debounce scheduling and Retrigger Work Cron Job "
        "integration are separate increments.",
    ):
        assert stale_claim not in local_cycle

    for required in (
        "`RuntimeEventBridge`",
        "`RuntimeEventMailbox`",
        "state-owning service loop",
    ):
        assert required in normalized_routine

    for stale_claim in (
        "does not yet attach asynchronous event consumption",
        "intended for future cross-context event transport",
        "A future transport owner",
        "Those ownership decisions remain a separate service-integration gate.",
    ):
        assert stale_claim not in normalized_routine


def test_slice_era_documents_match_current_integration_status() -> None:
    core = " ".join((ROOT / "docs/runtime-core-bundle.md").read_text().split())
    database = " ".join((ROOT / "docs/database-sync.md").read_text().split())
    history = " ".join((ROOT / "docs/database-history-replacement.md").read_text().split())
    prepared = " ".join((ROOT / "docs/prepared-deployment-state.md").read_text().split())

    for required in ("integrations", "floors", "labels", "recovery"):
        assert required in core
    assert (
        "Integrations/config entries, floors, labels, Supervisor data, hardware data, "
        "topology/dependency analysis and deployment observation remain separate future "
        "increments." not in core
    )

    for required in (
        "## Current integration status",
        "`DatabaseSyncService`",
        "`run_database_sync_retrigger_pass()`",
        "`database_retention`",
    ):
        assert required in database
    assert (
        "This increment does not implement database scheduling, retention pruning" not in database
    )

    assert "## Current integration status" in history
    assert (
        "Service orchestration and durable Retrigger scheduling are separate increments."
        not in history
    )

    for required in (
        "## Current integration status",
        "`candidate_apply`",
        "`candidate_apply_execute`",
        "rollback",
    ):
        assert required in prepared
    assert (
        "Runtime publication of these records and interrupted deployment execution remain "
        "future integration work." not in prepared
    )


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
    assert "openssh-client" in dockerfile
    assert "COPY github_known_hosts /app/github_known_hosts" in dockerfile
    assert (ROOT / "syncapp/github_known_hosts").is_file()
