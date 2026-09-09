import json
from dataclasses import replace
from pathlib import Path

import pytest
from ha_syncapp.runtime_inventory import (
    RuntimeInventoryError,
    RuntimeInventoryInput,
    build_runtime_inventory,
    verify_runtime_inventory,
)

COMMIT = "a" * 40


def _staging(tmp_path: Path) -> Path:
    root = tmp_path / "runtime-staging"
    root.mkdir()
    return root


def _inventory() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={"home_assistant_version": "2026.9.0", "entity_count": 2},
        homeassistant={
            "entities": [
                {"entity_id": "light.kitchen", "device_id": "device-1"},
                {"entity_id": "sensor.temperature", "device_id": "device-2"},
            ],
            "devices": [{"id": "device-1"}, {"id": "device-2"}],
        },
        analysis={"unavailable_entities": ["sensor.temperature"]},
    )


def test_build_emits_complete_readme_runtime_layout(tmp_path: Path) -> None:
    artifact = build_runtime_inventory(_staging(tmp_path), _inventory())

    relative = {entry.path for entry in artifact.files}
    assert "manifest.json" in relative
    assert "homeassistant/entities.json" in relative
    assert "homeassistant/states.json" in relative
    assert "supervisor/system.json" in relative
    assert "hardware/network.json" in relative
    assert "analysis/integration_health.json" in relative
    assert len(artifact.artifact_id) == 64
    verify_runtime_inventory(artifact)


def test_output_is_canonical_json_and_deterministic(tmp_path: Path) -> None:
    first_root = _staging(tmp_path)
    first = build_runtime_inventory(first_root, _inventory())
    manifest = (first.root / "manifest.json").read_bytes()

    second_root = tmp_path / "other-staging"
    second_root.mkdir()
    second = build_runtime_inventory(second_root, _inventory())

    assert first.artifact_id == second.artifact_id
    assert first.files == second.files
    assert manifest == b'{"entity_count":2,"home_assistant_version":"2026.9.0"}\n'


def test_empty_sections_get_explicit_defaults(tmp_path: Path) -> None:
    artifact = build_runtime_inventory(
        _staging(tmp_path),
        RuntimeInventoryInput(manifest={}),
    )

    assert json.loads((artifact.root / "homeassistant/entities.json").read_text()) == []
    assert json.loads((artifact.root / "supervisor/system.json").read_text()) == {}
    assert json.loads((artifact.root / "analysis/topology.json").read_text()) == {}


def test_deployment_records_require_full_lowercase_commit_sha(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    artifact = build_runtime_inventory(
        staging,
        RuntimeInventoryInput(manifest={}, deployments={COMMIT: {"status": "success"}}),
    )
    assert json.loads((artifact.root / "deployments" / f"{COMMIT}.json").read_text()) == {
        "status": "success"
    }

    with pytest.raises(RuntimeInventoryError, match="commit is invalid"):
        build_runtime_inventory(
            staging,
            RuntimeInventoryInput(manifest={}, deployments={"../candidate": {}}),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": b"bytes"},
        {1: "non-string-key"},
    ],
)
def test_non_json_safe_values_are_rejected(tmp_path: Path, payload: object) -> None:
    with pytest.raises(RuntimeInventoryError):
        build_runtime_inventory(
            _staging(tmp_path),
            RuntimeInventoryInput(manifest=payload),  # type: ignore[arg-type]
        )


def test_unknown_dataset_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeInventoryError, match="unsupported dataset keys"):
        build_runtime_inventory(
            _staging(tmp_path),
            RuntimeInventoryInput(manifest={}, homeassistant={"../../secret": []}),
        )


def test_verify_detects_modified_inserted_and_deleted_files(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    modified = build_runtime_inventory(staging, _inventory())
    (modified.root / "manifest.json").write_text("{}\n")
    with pytest.raises(RuntimeInventoryError, match="staged bytes"):
        verify_runtime_inventory(modified)

    inserted_root = tmp_path / "inserted"
    inserted_root.mkdir()
    inserted = build_runtime_inventory(inserted_root, _inventory())
    (inserted.root / "homeassistant" / "unexpected.json").write_text("{}\n")
    with pytest.raises(RuntimeInventoryError, match="layout"):
        verify_runtime_inventory(inserted)

    deleted_root = tmp_path / "deleted"
    deleted_root.mkdir()
    deleted = build_runtime_inventory(deleted_root, _inventory())
    (deleted.root / "hardware" / "network.json").unlink()
    with pytest.raises(RuntimeInventoryError, match="layout"):
        verify_runtime_inventory(deleted)


def test_verify_rejects_symlink_and_evidence_tampering(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    artifact = build_runtime_inventory(staging, _inventory())
    target = artifact.root / "manifest.json"
    target.unlink()
    target.symlink_to(artifact.root / "homeassistant" / "entities.json")
    with pytest.raises(RuntimeInventoryError, match="unsafe file"):
        verify_runtime_inventory(artifact)

    other_root = tmp_path / "evidence"
    other_root.mkdir()
    clean = build_runtime_inventory(other_root, _inventory())
    tampered = replace(clean, artifact_id="0" * 64)
    with pytest.raises(RuntimeInventoryError, match="evidence is inconsistent"):
        verify_runtime_inventory(tampered)


def test_artifact_collision_preserves_existing_verified_bytes(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    first = build_runtime_inventory(staging, _inventory())
    before = (first.root / "manifest.json").read_bytes()

    with pytest.raises(RuntimeInventoryError, match="already exists"):
        build_runtime_inventory(staging, _inventory())

    assert first.root.is_dir()
    assert (first.root / "manifest.json").read_bytes() == before
    verify_runtime_inventory(first)


def test_staging_root_must_be_existing_absolute_directory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeInventoryError, match="staging root"):
        build_runtime_inventory(Path("relative"), _inventory())

    file_root = tmp_path / "not-directory"
    file_root.write_text("x")
    with pytest.raises(RuntimeInventoryError, match="safe directory"):
        build_runtime_inventory(file_root, _inventory())
