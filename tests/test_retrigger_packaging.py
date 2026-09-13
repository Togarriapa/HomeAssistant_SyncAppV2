from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_packaging_enables_bounded_non_optional_retrigger_cadence() -> None:
    manifest = yaml.safe_load((ROOT / "syncapp/config.yaml").read_text())
    assert manifest["options"]["retrigger_interval_seconds"] == 300
    assert manifest["schema"]["retrigger_interval_seconds"] == "int(30,3600)"


def test_container_enters_scheduler_aware_state_owner_service() -> None:
    dockerfile = (ROOT / "syncapp/Dockerfile").read_text()
    assert 'ENTRYPOINT ["/opt/syncapp/bin/python", "-m", "ha_syncapp.service_entry"]' in dockerfile


def test_operator_translation_states_retrigger_schedule_cannot_be_disabled() -> None:
    translations = yaml.safe_load((ROOT / "syncapp/translations/en.yaml").read_text())
    option = translations["configuration"]["retrigger_interval_seconds"]
    assert "cannot be disabled" in option["description"]
