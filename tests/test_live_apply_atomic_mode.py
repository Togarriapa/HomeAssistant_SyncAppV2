from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ha_syncapp import live_apply_atomic_mode
from ha_syncapp.live_apply_atomic_mode import (
    LiveApplyAtomicModeBaselineMismatch,
    commit_verified_mode_leaf,
)


def _blob_id(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def test_verified_mode_changes_exact_baseline(tmp_path: Path) -> None:
    target = tmp_path / "script.sh"
    target.write_bytes(b"echo safe\n")
    os.chmod(target, 0o644)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        commit_verified_mode_leaf(
            parent_fd,
            target.name,
            expected_object_id=_blob_id(b"echo safe\n"),
            expected_mode="100644",
            target_mode="100755",
        )
    finally:
        os.close(parent_fd)
    assert target.read_bytes() == b"echo safe\n"
    assert target.stat().st_mode & 0o777 == 0o755


def test_mode_mismatch_preserves_changed_leaf(tmp_path: Path) -> None:
    target = tmp_path / "script.sh"
    target.write_bytes(b"external replacement\n")
    os.chmod(target, 0o644)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(LiveApplyAtomicModeBaselineMismatch):
            commit_verified_mode_leaf(
                parent_fd,
                target.name,
                expected_object_id=_blob_id(b"echo safe\n"),
                expected_mode="100644",
                target_mode="100755",
            )
    finally:
        os.close(parent_fd)
    assert target.read_bytes() == b"external replacement\n"
    assert target.stat().st_mode & 0o777 == 0o644


def test_mode_name_loss_after_fchmod_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "script.sh"
    target.write_bytes(b"echo safe\n")
    os.chmod(target, 0o644)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    def missing_live_name(*args: object, **kwargs: object) -> os.stat_result:
        raise FileNotFoundError

    monkeypatch.setattr(live_apply_atomic_mode.os, "stat", missing_live_name)
    try:
        with pytest.raises(live_apply_atomic_mode.LiveApplyAtomicModeOutcomeUncertain):
            commit_verified_mode_leaf(
                parent_fd,
                target.name,
                expected_object_id=_blob_id(b"echo safe\n"),
                expected_mode="100644",
                target_mode="100755",
            )
    finally:
        os.close(parent_fd)

    # fchmod already happened before live-name verification; this must never be
    # classified as a deterministic pre-mutation baseline mismatch.
    assert target.stat().st_mode & 0o777 == 0o755
