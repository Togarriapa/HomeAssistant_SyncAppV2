from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.haos_release_evidence import (
    HaosReleaseEvidenceError,
    main,
    validate_haos_release_evidence,
)

NOW = datetime(2026, 9, 29, 0, 45, tzinfo=UTC)


def _valid() -> dict[str, object]:
    return {
        "schema_version": 1,
        "observation_id": "123e4567-e89b-42d3-a456-426614174000",
        "observed_at": "2026-09-29T00:30:00Z",
        "build": {
            "syncapp_commit": "a" * 40,
            "image_sha256": "b" * 64,
        },
        "platform": {
            "architecture": "aarch64",
            "hardware": "raspberry_pi_5",
            "haos_version": "17.0",
            "supervisor_version": "2026.09.2",
            "core_version": "2026.9.3",
        },
        "security": {
            "parent_apparmor_label": "local_homeassistant_syncapp_v2 (enforce)",
            "validator_apparmor_label": ("local_homeassistant_syncapp_v2//validator (enforce)"),
            "protection_mode_enabled": True,
            "homeassistant_mount_read_only": True,
            "undocumented_privileges_absent": True,
        },
        "lifecycle": {
            "app_discoverable": True,
            "app_installed": True,
            "state_persisted_after_restart": True,
            "state_persisted_after_reboot": True,
            "retrigger_resumed_after_reboot": True,
            "single_state_owner_after_reboot": True,
            "sigterm_shutdown_clean": True,
            "invalid_options_rejected": True,
            "public_repository_rejected": True,
            "mismatched_repository_rejected": True,
        },
        "validator": {
            "valid_candidate_accepted": True,
            "invalid_candidate_rejected": True,
            "warning_candidate_rejected": True,
            "live_config_read_denied": True,
            "durable_data_read_denied": True,
            "unrelated_tmp_read_denied": True,
            "ipv4_socket_denied": True,
            "ipv6_socket_denied": True,
            "alternate_executable_denied": True,
            "profile_enforced_after_restart": True,
            "missing_profile_failed_closed": True,
        },
    }


def test_complete_physical_evidence_has_stable_content_free_summary() -> None:
    raw = json.dumps(_valid(), indent=2)
    result = validate_haos_release_evidence(raw, reference_time=NOW)
    reordered = validate_haos_release_evidence(
        json.dumps(_valid(), sort_keys=True), reference_time=NOW
    )

    assert result.passed is True
    assert result.check_count == 26
    assert result.failed_count == 0
    assert result.sha256 == reordered.sha256
    assert result.summary() == {
        "schema_version": 1,
        "passed": True,
        "check_count": 26,
        "failed_count": 0,
        "evidence_sha256": result.sha256,
        "gates": {
            "application_lifecycle": {
                "passed": True,
                "check_count": 14,
                "failed_count": 0,
            },
            "validator_confinement": {
                "passed": True,
                "check_count": 12,
                "failed_count": 0,
            },
        },
    }
    summary = json.dumps(result.summary(), sort_keys=True)
    assert "123e4567" not in summary
    assert "raspberry" not in summary
    assert "homeassistant_syncapp" not in summary


def test_failed_observation_is_valid_evidence_but_not_a_release_pass() -> None:
    evidence = _valid()
    lifecycle = evidence["lifecycle"]
    assert isinstance(lifecycle, dict)
    lifecycle["retrigger_resumed_after_reboot"] = False

    result = validate_haos_release_evidence(json.dumps(evidence), reference_time=NOW)

    assert result.passed is False
    assert result.failed_count == 1
    assert result.summary()["gates"] == {
        "application_lifecycle": {
            "passed": False,
            "check_count": 14,
            "failed_count": 1,
        },
        "validator_confinement": {
            "passed": True,
            "check_count": 12,
            "failed_count": 0,
        },
    }


def test_validator_failure_does_not_fail_application_lifecycle_gate() -> None:
    evidence = _valid()
    validator = evidence["validator"]
    assert isinstance(validator, dict)
    validator["ipv6_socket_denied"] = False

    result = validate_haos_release_evidence(json.dumps(evidence), reference_time=NOW)

    assert result.passed is False
    assert result.failed_count == 1
    assert result.summary()["gates"] == {
        "application_lifecycle": {
            "passed": True,
            "check_count": 14,
            "failed_count": 0,
        },
        "validator_confinement": {
            "passed": False,
            "check_count": 12,
            "failed_count": 1,
        },
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"token": "secret"}),
        lambda value: value["build"].update({"syncapp_commit": "A" * 40}),
        lambda value: value["platform"].update({"architecture": "amd64"}),
        lambda value: value["security"].update(
            {"validator_apparmor_label": "other//validator (enforce)"}
        ),
        lambda value: value["lifecycle"].update({"app_installed": 1}),
        lambda value: value["validator"].update({"diagnostic": "raw log"}),
    ],
)
def test_unknown_sensitive_or_noncanonical_evidence_is_rejected(mutate) -> None:
    evidence = deepcopy(_valid())
    mutate(evidence)

    with pytest.raises(HaosReleaseEvidenceError, match="invalid"):
        validate_haos_release_evidence(json.dumps(evidence), reference_time=NOW)


def test_duplicate_keys_and_nonfinite_values_are_rejected() -> None:
    duplicate = '{"schema_version":1,"schema_version":1}'
    with pytest.raises(HaosReleaseEvidenceError, match="invalid"):
        validate_haos_release_evidence(duplicate, reference_time=NOW)

    raw = json.dumps(_valid()).replace('"schema_version": 1', '"schema_version": NaN')
    with pytest.raises(HaosReleaseEvidenceError, match="invalid"):
        validate_haos_release_evidence(raw, reference_time=NOW)


def test_future_or_non_utc_observation_is_rejected() -> None:
    for observed_at in (
        (NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "2026-09-29T01:30:00+01:00",
    ):
        evidence = _valid()
        evidence["observed_at"] = observed_at
        with pytest.raises(HaosReleaseEvidenceError, match="invalid"):
            validate_haos_release_evidence(json.dumps(evidence), reference_time=NOW)


def test_oversized_or_non_utf8_evidence_is_rejected() -> None:
    with pytest.raises(HaosReleaseEvidenceError, match="invalid"):
        validate_haos_release_evidence(b"\xff", reference_time=NOW)
    with pytest.raises(HaosReleaseEvidenceError, match="invalid"):
        validate_haos_release_evidence(b" " * 32769, reference_time=NOW)


def test_cli_emits_only_content_free_summary_for_pass_and_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence = _valid()
    evidence_path.write_text(json.dumps(evidence))

    assert main([str(evidence_path)], reference_time=NOW) == 0
    passed = json.loads(capsys.readouterr().out)
    assert passed["passed"] is True
    assert set(passed) == {
        "schema_version",
        "passed",
        "check_count",
        "failed_count",
        "evidence_sha256",
        "gates",
    }
    assert set(passed["gates"]) == {
        "application_lifecycle",
        "validator_confinement",
    }

    lifecycle = evidence["lifecycle"]
    assert isinstance(lifecycle, dict)
    lifecycle["app_installed"] = False
    evidence_path.write_text(json.dumps(evidence))

    assert main([str(evidence_path)], reference_time=NOW) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["passed"] is False
    assert failed["failed_count"] == 1


def test_cli_sanitizes_invalid_document_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text('{"token":"secret-sentinel"}')

    assert main([str(evidence_path)], reference_time=NOW) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "HAOS release evidence is invalid\n"
    assert "secret-sentinel" not in output.err


def test_runbook_defines_issue_specific_content_free_review() -> None:
    runbook = (Path(__file__).resolve().parents[1] / "docs/haos-release-evidence.md").read_text()

    for required in (
        "application_lifecycle",
        "validator_confinement",
        "issues/212",
        "issues/251",
        "does not prove",
        "original evidence document remains private",
    ):
        assert required in runbook
