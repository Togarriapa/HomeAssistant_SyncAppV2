from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from ha_syncapp import live_apply_writer
from ha_syncapp.live_apply_plan import LiveApplyOperation


def _blob_id(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def test_parent_replacement_after_proof_boundary_is_rejected(tmp_path: Path) -> None:
    live = tmp_path / "homeassistant"
    parent = live / "packages"
    parent.mkdir(parents=True)
    target = parent / "automations.yaml"
    target.write_bytes(b"baseline\n")
    os.chmod(target, 0o644)

    operation = LiveApplyOperation(
        path="packages/automations.yaml",
        status="modified",
        baseline_mode="100644",
        baseline_object_id=_blob_id(b"baseline\n"),
        candidate_mode="100644",
        candidate_object_id=_blob_id(b"candidate\n"),
        staged_size=len(b"candidate\n"),
        staged_sha256=hashlib.sha256(b"candidate\n").hexdigest(),
    )

    expected_root_identity = live_apply_writer._directory_identity(live)
    expected_parent_identities = live_apply_writer._parent_directory_identities(
        live, operation.path
    )

    original_parent = live / "packages-original"
    parent.rename(original_parent)
    parent.mkdir()
    replacement = parent / "automations.yaml"
    replacement.write_bytes(b"baseline\n")
    os.chmod(replacement, 0o644)

    with pytest.raises(live_apply_writer._PreMutationMismatch):
        live_apply_writer._mutate_operation(
            live,
            operation,
            b"candidate\n",
            expected_root_identity,
            expected_parent_identities,
        )

    assert (original_parent / "automations.yaml").read_bytes() == b"baseline\n"
    assert replacement.read_bytes() == b"baseline\n"
