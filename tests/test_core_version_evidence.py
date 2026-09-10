from __future__ import annotations

from dataclasses import replace

import pytest
from ha_syncapp.core_version_evidence import (
    CoreVersionEvidenceError,
    bind_core_version,
    verify_core_version_evidence,
)
from ha_syncapp.runtime_inventory import RuntimeInventoryInput


def _runtime(version: object = "2026.9.1") -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={"core_config": {"version": version, "location_name": "Home"}},
        homeassistant={"states": []},
    )


def test_binds_exact_core_release_to_canonical_runtime() -> None:
    runtime = _runtime()

    evidence = bind_core_version(runtime)

    assert evidence.version == "2026.9.1"
    assert len(evidence.runtime_sha256) == 64
    verify_core_version_evidence(evidence, runtime)


@pytest.mark.parametrize(
    "version",
    [
        None,
        20260901,
        "",
        "2026.09.1",
        "2026.9.01",
        "2026.13.1",
        "2026.0.1",
        "2026.9",
        "2026.9.1 ",
        " 2026.9.1",
        "stable",
        "latest",
        "2026.9.0b1",
        "2026.9.0.dev0",
        "2026.9.0rc1",
    ],
)
def test_ambiguous_or_nonrelease_versions_fail_closed(version: object) -> None:
    with pytest.raises(CoreVersionEvidenceError, match="exact stable release"):
        bind_core_version(_runtime(version))


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"core_config": None},
        {"core_config": "2026.9.1"},
        {"version": "2026.9.1"},
    ],
)
def test_version_must_come_from_collected_core_config(manifest: object) -> None:
    runtime = RuntimeInventoryInput(manifest=manifest if isinstance(manifest, dict) else {})

    with pytest.raises(CoreVersionEvidenceError, match="unavailable"):
        bind_core_version(runtime)


def test_runtime_drift_invalidates_version_evidence() -> None:
    original = _runtime("2026.9.1")
    evidence = bind_core_version(original)
    changed = RuntimeInventoryInput(
        manifest=original.manifest,
        homeassistant={"states": [{"entity_id": "light.kitchen", "state": "on"}]},
    )

    with pytest.raises(CoreVersionEvidenceError, match="does not match"):
        verify_core_version_evidence(evidence, changed)


def test_version_change_invalidates_version_evidence() -> None:
    evidence = bind_core_version(_runtime("2026.9.1"))

    with pytest.raises(CoreVersionEvidenceError, match="does not match"):
        verify_core_version_evidence(evidence, _runtime("2026.9.2"))


def test_tampered_version_or_runtime_digest_is_rejected() -> None:
    runtime = _runtime()
    evidence = bind_core_version(runtime)

    with pytest.raises(CoreVersionEvidenceError):
        verify_core_version_evidence(replace(evidence, version="stable"), runtime)
    with pytest.raises(CoreVersionEvidenceError):
        verify_core_version_evidence(replace(evidence, runtime_sha256="0" * 64), runtime)


def test_binding_is_deterministic_and_does_not_mutate_runtime() -> None:
    runtime = _runtime()
    original_manifest = dict(runtime.manifest)
    original_homeassistant = dict(runtime.homeassistant)

    first = bind_core_version(runtime)
    second = bind_core_version(runtime)

    assert first == second
    assert runtime.manifest == original_manifest
    assert runtime.homeassistant == original_homeassistant
