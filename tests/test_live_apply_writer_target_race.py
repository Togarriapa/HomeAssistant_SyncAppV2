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


def test_modified_leaf_replacement_at_mutation_boundary_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed leaf must not be overwritten after the final baseline proof."""
    live = tmp_path / "homeassistant"
    live.mkdir()
    target = live / "automations.yaml"
    target.write_bytes(b"baseline\n")
    os.chmod(target, 0o644)

    operation = LiveApplyOperation(
        path="automations.yaml",
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

    real_replace = os.replace

    def replace_after_leaf_race(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        target.write_bytes(b"changed-after-proof\n")
        os.chmod(target, 0o644)
        return real_replace(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(live_apply_writer.os, "replace", replace_after_leaf_race)

    with pytest.raises(live_apply_writer._PreMutationMismatch):
        live_apply_writer._mutate_operation(
            live,
            operation,
            b"candidate\n",
            expected_root_identity,
            expected_parent_identities,
        )

    assert target.read_bytes() == b"changed-after-proof\n"
