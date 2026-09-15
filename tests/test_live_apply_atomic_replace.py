from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from ha_syncapp.live_apply_atomic_replace import (
    LiveApplyAtomicReplaceError,
    exchange_leaf,
    exchange_verified_baseline,
    verify_displaced_leaf,
)


def _blob_id(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def test_exchange_leaf_preserves_exact_displaced_object_for_verification(tmp_path: Path) -> None:
    parent = tmp_path / "homeassistant"
    parent.mkdir()
    target = parent / "automations.yaml"
    temporary = parent / ".syncapp-candidate.tmp"
    target.write_bytes(b"raced-live-bytes\n")
    temporary.write_bytes(b"candidate\n")

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        exchange_leaf(parent_fd, temporary.name, target.name)
    finally:
        os.close(parent_fd)

    assert target.read_bytes() == b"candidate\n"
    assert temporary.read_bytes() == b"raced-live-bytes\n"


def test_exchange_leaf_can_be_reversed_without_losing_raced_object(tmp_path: Path) -> None:
    parent = tmp_path / "homeassistant"
    parent.mkdir()
    target = parent / "automations.yaml"
    temporary = parent / ".syncapp-candidate.tmp"
    target.write_bytes(b"changed-after-proof\n")
    temporary.write_bytes(b"candidate\n")

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        exchange_leaf(parent_fd, temporary.name, target.name)
        exchange_leaf(parent_fd, temporary.name, target.name)
    finally:
        os.close(parent_fd)

    assert target.read_bytes() == b"changed-after-proof\n"
    assert temporary.read_bytes() == b"candidate\n"


def test_exchange_verified_baseline_keeps_candidate_for_exact_baseline(tmp_path: Path) -> None:
    baseline = b"baseline\n"
    parent = tmp_path / "homeassistant"
    parent.mkdir()
    target = parent / "automations.yaml"
    temporary = parent / ".syncapp-candidate.tmp"
    target.write_bytes(baseline)
    temporary.write_bytes(b"candidate\n")

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert exchange_verified_baseline(
            parent_fd,
            temporary.name,
            target.name,
            expected_object_id=_blob_id(baseline),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)

    assert target.read_bytes() == b"candidate\n"
    assert temporary.read_bytes() == baseline


def test_exchange_verified_baseline_restores_changed_target(tmp_path: Path) -> None:
    baseline = b"baseline\n"
    raced = b"changed-after-proof\n"
    parent = tmp_path / "homeassistant"
    parent.mkdir()
    target = parent / "automations.yaml"
    temporary = parent / ".syncapp-candidate.tmp"
    target.write_bytes(raced)
    temporary.write_bytes(b"candidate\n")

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert not exchange_verified_baseline(
            parent_fd,
            temporary.name,
            target.name,
            expected_object_id=_blob_id(baseline),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)

    assert target.read_bytes() == raced
    assert temporary.read_bytes() == b"candidate\n"


def test_verify_displaced_leaf_accepts_exact_baseline_identity_and_mode(tmp_path: Path) -> None:
    baseline = b"baseline\n"
    displaced = tmp_path / ".syncapp-displaced.tmp"
    displaced.write_bytes(baseline)
    os.chmod(displaced, 0o644)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert verify_displaced_leaf(
            parent_fd,
            displaced.name,
            expected_object_id=_blob_id(baseline),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)


def test_verify_displaced_leaf_rejects_raced_content_or_mode(tmp_path: Path) -> None:
    displaced = tmp_path / ".syncapp-displaced.tmp"
    displaced.write_bytes(b"changed-after-proof\n")
    os.chmod(displaced, 0o755)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert not verify_displaced_leaf(
            parent_fd,
            displaced.name,
            expected_object_id=_blob_id(b"baseline\n"),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)


def test_verify_displaced_leaf_fails_closed_for_symlink(tmp_path: Path) -> None:
    baseline = b"baseline\n"
    target = tmp_path / "target"
    target.write_bytes(baseline)
    displaced = tmp_path / ".syncapp-displaced.tmp"
    displaced.symlink_to(target)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert not verify_displaced_leaf(
            parent_fd,
            displaced.name,
            expected_object_id=_blob_id(baseline),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)


def test_exchange_leaf_rejects_path_components(tmp_path: Path) -> None:
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(LiveApplyAtomicReplaceError, match="leaf name is invalid"):
            exchange_leaf(parent_fd, "nested/candidate", "automations.yaml")
    finally:
        os.close(parent_fd)
