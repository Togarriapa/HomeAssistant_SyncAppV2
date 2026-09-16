from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ha_syncapp.live_apply_atomic_commit import commit_verified_modified_leaf
from ha_syncapp.live_apply_atomic_guard import LiveApplyAtomicBaselineMismatch


def _oid(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def test_commit_verified_modified_leaf_keeps_candidate_and_removes_displaced_baseline(
    tmp_path: Path,
) -> None:
    baseline = b"baseline\n"
    candidate = b"candidate\n"
    target = tmp_path / "configuration.yaml"
    temporary = tmp_path / ".candidate.tmp"
    target.write_bytes(baseline)
    temporary.write_bytes(candidate)
    os.chmod(target, 0o644)
    os.chmod(temporary, 0o644)

    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        commit_verified_modified_leaf(
            parent_fd,
            temporary.name,
            target.name,
            expected_object_id=_oid(baseline),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)

    assert target.read_bytes() == candidate
    assert not temporary.exists()


def test_commit_verified_modified_leaf_restores_raced_target(tmp_path: Path) -> None:
    baseline = b"baseline\n"
    raced = b"operator edit\n"
    candidate = b"candidate\n"
    target = tmp_path / "configuration.yaml"
    temporary = tmp_path / ".candidate.tmp"
    target.write_bytes(raced)
    temporary.write_bytes(candidate)
    os.chmod(target, 0o644)
    os.chmod(temporary, 0o644)

    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(LiveApplyAtomicBaselineMismatch):
            commit_verified_modified_leaf(
                parent_fd,
                temporary.name,
                target.name,
                expected_object_id=_oid(baseline),
                expected_mode="100644",
            )
    finally:
        os.close(parent_fd)

    assert target.read_bytes() == raced
    assert temporary.read_bytes() == candidate
