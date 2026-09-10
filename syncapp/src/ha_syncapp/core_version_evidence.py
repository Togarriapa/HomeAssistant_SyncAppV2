"""Exact running Home Assistant Core version evidence for candidate validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .runtime_evidence import RuntimeEvidenceError, fingerprint_runtime
from .runtime_inventory import RuntimeInventoryInput

_CORE_RELEASE = re.compile(r"^20[0-9]{2}\.(?:[1-9]|1[0-2])\.(?:0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CoreVersionEvidenceError(RuntimeError):
    """Running Home Assistant Core version evidence is absent or ambiguous."""


@dataclass(frozen=True, slots=True)
class CoreVersionEvidence:
    """Immutable exact release version bound to one canonical runtime snapshot."""

    version: str
    runtime_sha256: str


def bind_core_version(runtime: RuntimeInventoryInput) -> CoreVersionEvidence:
    """Extract an exact stable Core release from the collected runtime manifest."""
    if type(runtime) is not RuntimeInventoryInput:
        raise CoreVersionEvidenceError("Home Assistant Core version runtime evidence is invalid")
    version = _extract_core_version(runtime.manifest)
    try:
        runtime_sha256 = fingerprint_runtime(runtime)
    except RuntimeEvidenceError as exc:
        raise CoreVersionEvidenceError(
            "Home Assistant Core version runtime evidence is invalid"
        ) from exc
    evidence = CoreVersionEvidence(version=version, runtime_sha256=runtime_sha256)
    _validate_evidence(evidence)
    return evidence


def verify_core_version_evidence(
    evidence: CoreVersionEvidence,
    runtime: RuntimeInventoryInput,
) -> None:
    """Recompute exact version/runtime evidence and reject drift or tampering."""
    _validate_evidence(evidence)
    expected = bind_core_version(runtime)
    if evidence != expected:
        raise CoreVersionEvidenceError(
            "Home Assistant Core version evidence does not match runtime"
        )


def _extract_core_version(manifest: Mapping[str, object]) -> str:
    core_config = manifest.get("core_config")
    if not isinstance(core_config, Mapping):
        raise CoreVersionEvidenceError("Home Assistant Core version is unavailable")
    version = core_config.get("version")
    if not isinstance(version, str) or _CORE_RELEASE.fullmatch(version) is None:
        raise CoreVersionEvidenceError("Home Assistant Core version is not an exact stable release")
    return version


def _validate_evidence(evidence: CoreVersionEvidence) -> None:
    if (
        type(evidence) is not CoreVersionEvidence
        or _CORE_RELEASE.fullmatch(evidence.version) is None
        or _SHA256.fullmatch(evidence.runtime_sha256) is None
    ):
        raise CoreVersionEvidenceError("Home Assistant Core version evidence is invalid")
