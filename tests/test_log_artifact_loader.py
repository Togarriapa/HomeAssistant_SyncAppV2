from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp.log_artifact import LogRecord, build_log_artifact
from ha_syncapp.log_artifact_loader import LogArtifactLoadError, load_log_artifact

REFERENCE = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


def _artifact(root: Path):
    return build_log_artifact(
        root,
        (
            LogRecord(
                category="syncapp",
                record_id="one",
                timestamp=REFERENCE,
                message="message",
            ),
        ),
        reference_time=REFERENCE,
    )


def test_loads_and_reverifies_exact_manifest_derived_artifact(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    created = _artifact(root)

    loaded = load_log_artifact(root, created.artifact_id)

    assert loaded == created
    assert loaded.root == root / created.artifact_id


def test_rejects_invalid_or_missing_artifact_identifier(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)

    with pytest.raises(LogArtifactLoadError):
        load_log_artifact(root, "../artifact")
    with pytest.raises(LogArtifactLoadError):
        load_log_artifact(root, "0" * 64)


def test_rejects_artifact_outside_private_configured_root(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    created = _artifact(private)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)

    with pytest.raises(LogArtifactLoadError):
        load_log_artifact(unsafe, created.artifact_id)


def test_rejects_tampered_manifest_even_when_artifact_id_is_supplied(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    created = _artifact(root)
    manifest = created.root / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")

    with pytest.raises(LogArtifactLoadError):
        load_log_artifact(root, created.artifact_id)


def test_rejects_tampered_payload_after_manifest_reconstruction(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    created = _artifact(root)
    payload = created.root / "logs/syncapp/records.jsonl"
    payload.write_text("tampered\n")
    payload.chmod(0o600)

    with pytest.raises(LogArtifactLoadError):
        load_log_artifact(root, created.artifact_id)
