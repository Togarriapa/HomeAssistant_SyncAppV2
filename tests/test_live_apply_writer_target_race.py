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


def _operation(*, status: str, candidate_mode: str | None = "100644") -> LiveApplyOperation:
    candidate = None if status == "deleted" else b"baseline\n"
    return LiveApplyOperation(
        path="automations.yaml",
        status=status,
        baseline_mode="100644",
        baseline_object_id=_blob_id(b"baseline\n"),
        candidate_mode=candidate_mode,
        candidate_object_id=None if candidate is None else _blob_id(candidate),
        staged_size=None if candidate is None else len(candidate),
        staged_sha256=None if candidate is None else hashlib.sha256(candidate).hexdigest(),
    )


def _identities(live: Path, operation: LiveApplyOperation):
    return (
        live_apply_writer._directory_identity(live),
        live_apply_writer._parent_directory_identities(live, operation.path),
    )


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
    expected_root_identity, expected_parent_identities = _identities(live, operation)

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


def test_deleted_leaf_replacement_at_mutation_boundary_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete must never unlink a leaf that no longer matches the authorized baseline."""
    live = tmp_path / "homeassistant"
    live.mkdir()
    target = live / "automations.yaml"
    target.write_bytes(b"baseline\n")
    os.chmod(target, 0o644)
    operation = _operation(status="deleted", candidate_mode=None)
    expected_root_identity, expected_parent_identities = _identities(live, operation)
    real_unlink = os.unlink
    raced = False

    def unlink_after_leaf_race(path, *, dir_fd=None):
        nonlocal raced
        if path == "automations.yaml" and not raced:
            raced = True
            target.write_bytes(b"changed-after-proof\n")
            os.chmod(target, 0o644)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(live_apply_writer.os, "unlink", unlink_after_leaf_race)

    with pytest.raises(live_apply_writer._PreMutationMismatch):
        live_apply_writer._mutate_operation(
            live,
            operation,
            None,
            expected_root_identity,
            expected_parent_identities,
        )

    assert target.read_bytes() == b"changed-after-proof\n"


def test_mode_changed_leaf_replacement_at_mutation_boundary_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mode change must never chmod a leaf that no longer matches the authorized baseline."""
    live = tmp_path / "homeassistant"
    live.mkdir()
    target = live / "automations.yaml"
    target.write_bytes(b"baseline\n")
    os.chmod(target, 0o644)
    operation = _operation(status="mode_changed", candidate_mode="100755")
    expected_root_identity, expected_parent_identities = _identities(live, operation)
    real_open = os.open
    raced = False

    def open_after_leaf_race(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if path == "automations.yaml" and dir_fd is not None and not raced:
            raced = True
            target.write_bytes(b"changed-after-proof\n")
            os.chmod(target, 0o644)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(live_apply_writer.os, "open", open_after_leaf_race)

    with pytest.raises(live_apply_writer._PreMutationMismatch):
        live_apply_writer._mutate_operation(
            live,
            operation,
            None,
            expected_root_identity,
            expected_parent_identities,
        )

    assert target.read_bytes() == b"changed-after-proof\n"
    assert target.stat().st_mode & 0o777 == 0o644
