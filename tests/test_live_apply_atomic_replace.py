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


def test_verify_displaced_leaf_accepts_expected_regular_file(tmp_path: Path) -> None:
    parent = tmp_path / "homeassistant"
    parent.mkdir()
    displaced = parent / ".syncapp-candidate.tmp"
    baseline = b"baseline\n"
    displaced.write_bytes(baseline)

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        verified = verify_displaced_leaf(
            parent_fd,
            displaced.name,
            expected_object_id=_blob_id(baseline),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)

    assert verified is True


def test_verify_displaced_leaf_fails_closed_for_content_race(tmp_path: Path) -> None:
    parent = tmp_path / "homeassistant"
    parent.mkdir()
    displaced = parent / ".syncapp-candidate.tmp"
    displaced.write_bytes(b"raced\n")

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        verified = verify_displaced_leaf(
            parent_fd,
            displaced.name,
            expected_object_id=_blob_id(b"baseline\n"),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)

    assert verified is False


def test_verify_displaced_leaf_fails_closed_for_symlink(tmp_path: Path) -> None:
    parent = tmp_path / "homeassistant"
    parent.mkdir()
    baseline = b"baseline\n"
    elsewhere = parent / "elsewhere.yaml"
    elsewhere.write_bytes(baseline)
    displaced = parent / ".syncapp-candidate.tmp"
    displaced.symlink_to(elsewhere.name)

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        verified = verify_displaced_leaf(
            parent_fd,
            displaced.name,
            expected_object_id=_blob_id(baseline),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)

    assert verified is False


def test_exchange_verified_baseline_restores_displaced_file_when_baseline_mismatches(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "homeassistant"
    parent.mkdir()
    target = parent / "automations.yaml"
    temporary = parent / ".syncapp-candidate.tmp"
    raced = b"raced-live-bytes\n"
    target.write_bytes(raced)
    temporary.write_bytes(b"candidate\n")

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        committed = exchange_verified_baseline(
            parent_fd,
            temporary.name,
            target.name,
            expected_object_id=_blob_id(b"expected-baseline\n"),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)

    assert committed is False
    assert target.read_bytes() == raced
    assert temporary.read_bytes() == b"candidate\n"


def test_exchange_verified_baseline_commits_candidate_when_baseline_matches(tmp_path: Path) -> None:
    parent = tmp_path / "homeassistant"
    parent.mkdir()
    target = parent / "automations.yaml"
    temporary = parent / ".syncapp-candidate.tmp"
    baseline = b"baseline\n"
    target.write_bytes(baseline)
    temporary.write_bytes(b"candidate\n")

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        committed = exchange_verified_baseline(
            parent_fd,
            temporary.name,
            target.name,
            expected_object_id=_blob_id(baseline),
            expected_mode="100644",
        )
    finally:
        os.close(parent_fd)

    assert committed is True
    assert target.read_bytes() == b"candidate\n"
    assert temporary.read_bytes() == baseline


def test_exchange_leaf_rejects_invalid_parent_descriptor() -> None:
    with pytest.raises(LiveApplyAtomicReplaceError, match="parent directory descriptor is invalid"):
        exchange_leaf(-1, ".syncapp-candidate.tmp", "automations.yaml")
