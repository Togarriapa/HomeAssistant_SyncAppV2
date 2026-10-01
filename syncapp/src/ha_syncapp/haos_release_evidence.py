"""Strict, content-safe contract for physical HAOS release evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

_MAX_BYTES = 32 * 1024
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_HAOS_VERSION = re.compile(r"^[1-9][0-9]*\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?$")
_CALENDAR_VERSION = re.compile(r"^20[0-9]{2}\.(?:0?[1-9]|1[0-2])\.(?:0|[1-9][0-9]*)$")
_PARENT_PROFILE = re.compile(r"^(?:[A-Za-z0-9_-]+_)?homeassistant_syncapp_v2 \(enforce\)$")
_VALIDATOR_PROFILE = re.compile(
    r"^(?:[A-Za-z0-9_-]+_)?homeassistant_syncapp_v2//validator \(enforce\)$"
)
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "observation_id",
        "observed_at",
        "build",
        "platform",
        "security",
        "lifecycle",
        "validator",
    }
)
_BUILD_KEYS = frozenset({"syncapp_commit", "image_sha256"})
_PLATFORM_KEYS = frozenset(
    {"architecture", "hardware", "haos_version", "supervisor_version", "core_version"}
)
_SECURITY_KEYS = frozenset(
    {
        "parent_apparmor_label",
        "validator_apparmor_label",
        "protection_mode_enabled",
        "homeassistant_mount_read_only",
        "undocumented_privileges_absent",
    }
)
_LIFECYCLE_KEYS = frozenset(
    {
        "app_discoverable",
        "app_installed",
        "state_persisted_after_restart",
        "state_persisted_after_reboot",
        "retrigger_resumed_after_reboot",
        "single_state_owner_after_reboot",
        "sigterm_shutdown_clean",
        "invalid_options_rejected",
        "public_repository_rejected",
        "mismatched_repository_rejected",
    }
)
_VALIDATOR_KEYS = frozenset(
    {
        "valid_candidate_accepted",
        "invalid_candidate_rejected",
        "warning_candidate_rejected",
        "live_config_read_denied",
        "durable_data_read_denied",
        "unrelated_tmp_read_denied",
        "ipv4_socket_denied",
        "ipv6_socket_denied",
        "alternate_executable_denied",
        "profile_enforced_after_restart",
        "missing_profile_failed_closed",
    }
)


class HaosReleaseEvidenceError(RuntimeError):
    """Physical evidence is malformed, ambiguous, or unsafe to retain."""


@dataclass(frozen=True, slots=True)
class HaosReleaseEvidence:
    """Content-free result of validating one physical evidence document."""

    sha256: str
    lifecycle_check_count: int
    lifecycle_failed_count: int
    validator_check_count: int
    validator_failed_count: int

    @property
    def check_count(self) -> int:
        return self.lifecycle_check_count + self.validator_check_count

    @property
    def failed_count(self) -> int:
        return self.lifecycle_failed_count + self.validator_failed_count

    @property
    def passed(self) -> bool:
        return self.failed_count == 0

    def summary(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "passed": self.passed,
            "check_count": self.check_count,
            "failed_count": self.failed_count,
            "evidence_sha256": self.sha256,
            "gates": {
                "application_lifecycle": _gate_summary(
                    self.lifecycle_check_count,
                    self.lifecycle_failed_count,
                ),
                "validator_confinement": _gate_summary(
                    self.validator_check_count,
                    self.validator_failed_count,
                ),
            },
        }


def validate_haos_release_evidence(
    document: str | bytes,
    *,
    reference_time: datetime | None = None,
) -> HaosReleaseEvidence:
    """Validate one bounded physical observation without echoing its content."""
    try:
        raw = _document_bytes(document)
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        root = _exact_object(parsed, _ROOT_KEYS)
        if root["schema_version"] != 1 or type(root["schema_version"]) is not int:
            _invalid()
        _observation_id(root["observation_id"])
        observed_at = _observed_at(root["observed_at"])
        now = _reference_time(reference_time)
        if observed_at > now:
            _invalid()

        build = _exact_object(root["build"], _BUILD_KEYS)
        if _COMMIT.fullmatch(_string(build["syncapp_commit"])) is None:
            _invalid()
        if _HASH.fullmatch(_string(build["image_sha256"])) is None:
            _invalid()

        platform = _exact_object(root["platform"], _PLATFORM_KEYS)
        if (
            platform["architecture"] != "aarch64"
            or platform["hardware"] != "raspberry_pi_5"
            or _HAOS_VERSION.fullmatch(_string(platform["haos_version"])) is None
            or _CALENDAR_VERSION.fullmatch(_string(platform["supervisor_version"])) is None
            or _CALENDAR_VERSION.fullmatch(_string(platform["core_version"])) is None
        ):
            _invalid()

        security = _exact_object(root["security"], _SECURITY_KEYS)
        parent = _string(security["parent_apparmor_label"])
        validator = _string(security["validator_apparmor_label"])
        if (
            _PARENT_PROFILE.fullmatch(parent) is None
            or _VALIDATOR_PROFILE.fullmatch(validator) is None
            or validator != f"{parent.removesuffix(' (enforce)')}//validator (enforce)"
        ):
            _invalid()

        lifecycle_checks = [
            True,
            *(
                _boolean(security[key])
                for key in sorted(
                    _SECURITY_KEYS
                    - {
                        "parent_apparmor_label",
                        "validator_apparmor_label",
                    }
                )
            ),
        ]
        lifecycle = _exact_object(root["lifecycle"], _LIFECYCLE_KEYS)
        lifecycle_checks.extend(_boolean(lifecycle[key]) for key in sorted(_LIFECYCLE_KEYS))
        validator_checks = _exact_object(root["validator"], _VALIDATOR_KEYS)
        confinement_checks = [True]
        confinement_checks.extend(
            _boolean(validator_checks[key]) for key in sorted(_VALIDATOR_KEYS)
        )

        canonical = json.dumps(
            parsed,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return HaosReleaseEvidence(
            hashlib.sha256(canonical).hexdigest(),
            len(lifecycle_checks),
            sum(not check for check in lifecycle_checks),
            len(confinement_checks),
            sum(not check for check in confinement_checks),
        )
    except HaosReleaseEvidenceError:
        raise
    except (AttributeError, UnicodeError, ValueError, TypeError, KeyError, OverflowError):
        _invalid()


def main(
    argv: Sequence[str] | None = None,
    *,
    reference_time: datetime | None = None,
) -> int:
    """Validate a local document and print only its content-free summary."""
    parser = argparse.ArgumentParser(description="Validate physical HAOS release evidence")
    parser.add_argument("evidence", type=Path)
    arguments = parser.parse_args(argv)
    try:
        with arguments.evidence.open("rb") as stream:
            document = stream.read(_MAX_BYTES + 1)
        result = validate_haos_release_evidence(document, reference_time=reference_time)
    except (OSError, HaosReleaseEvidenceError):
        print("HAOS release evidence is invalid", file=sys.stderr)
        return 2
    print(json.dumps(result.summary(), sort_keys=True, separators=(",", ":")))
    return 0 if result.passed else 1


def _document_bytes(document: str | bytes) -> bytes:
    if isinstance(document, str):
        raw = document.encode("utf-8")
    elif type(document) is bytes:
        raw = document
    else:
        _invalid()
    if not raw or len(raw) > _MAX_BYTES:
        _invalid()
    return raw


def _gate_summary(check_count: int, failed_count: int) -> dict[str, object]:
    return {
        "passed": failed_count == 0,
        "check_count": check_count,
        "failed_count": failed_count,
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _invalid_constant(_value: str) -> NoReturn:
    _invalid()


def _exact_object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _invalid()
    return value


def _string(value: object) -> str:
    if type(value) is not str:
        _invalid()
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        _invalid()
    return value


def _observation_id(value: object) -> None:
    candidate = _string(value)
    parsed = uuid.UUID(candidate)
    if parsed.version != 4 or str(parsed) != candidate:
        _invalid()


def _observed_at(value: object) -> datetime:
    candidate = _string(value)
    if not candidate.endswith("Z"):
        _invalid()
    parsed = datetime.fromisoformat(candidate.removesuffix("Z") + "+00:00")
    if parsed.tzinfo != UTC or parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != candidate:
        _invalid()
    return parsed


def _reference_time(value: datetime | None) -> datetime:
    candidate = datetime.now(UTC) if value is None else value
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        _invalid()
    return candidate.astimezone(UTC)


def _invalid() -> NoReturn:
    raise HaosReleaseEvidenceError("HAOS release evidence is invalid") from None


if __name__ == "__main__":
    raise SystemExit(main())
