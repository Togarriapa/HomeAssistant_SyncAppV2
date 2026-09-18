from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ha_syncapp.live_apply_atomic_delete import (
    LiveApplyAtomicDeleteBaselineMismatch,
    LiveApplyAtomicDeleteError,
    commit_verified_deleted_leaf,
)


def _blob_id(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def test_verified_delete_removes_exact_baseline(tmp_path: Path) -> None:
    target = tmp_path / "automations.yaml"
    target.write_bytes(b"baseline\n")
    os.chmod(target, 0o644)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        commit_verified_deleted_leaf(
            parent_fd,
            target.name,
            expected_object_id=_blob_id(b"baseline\n"),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_delete_mismatch_restores_changed_leaf(tmp_path: Path) -> None:
    target = tmp_path / "automations.yaml"
    target.write_bytes(b"changed-after-proof\n")
    os.chmod(target, 0o644)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(LiveApplyAtomicDeleteBaselineMismatch):
            commit_verified_deleted_leaf(
                parent_fd,
                target.name,
                expected_object_id=_blob_id(b"baseline\n"),
                expected_mode="100644",
            )
    finally:
        os.close(parent_fd)
    assert target.read_bytes() == b"changed-after-proof\n"
    assert target.stat().st_mode & 0o777 == 0o644
    assert list(tmp_path.iterdir()) == [target]


def test_delete_missing_at_mutation_boundary_is_baseline_mismatch(tmp_path: Path) -> None:
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(LiveApplyAtomicDeleteBaselineMismatch):
            commit_verified_deleted_leaf(
                parent_fd,
                "automations.yaml",
                expected_object_id=_blob_id(b"baseline\n"),
                expected_mode="100644",
            )
    finally:
        os.close(parent_fd)
    assert list(tmp_path.iterdir()) == []


def test_delete_rejects_invalid_baseline_identity_before_mutation(tmp_path: Path) -> None:
    target = tmp_path / "automations.yaml"
    target.write_bytes(b"baseline\n")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(LiveApplyAtomicDeleteError):
            commit_verified_deleted_leaf(
                parent_fd,
                target.name,
                expected_object_id="not-a-git-object-id",
                expected_mode="100644",
            )
    finally:
        os.close(parent_fd)
    assert target.read_bytes() == b"baseline\n"
    assert list(tmp_path.iterdir()) == [target]
