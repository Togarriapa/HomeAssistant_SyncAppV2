from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from ha_syncapp.live_apply_atomic_guard import (
    LiveApplyAtomicGuardError,
    exchange_verified_baseline,
)


def _blob_id(data: bytes) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def test_exchange_verified_baseline_keeps_candidate_for_exact_baseline(tmp_path: Path) -> None:
    baseline = b"baseline\n"
    target = tmp_path / "automations.yaml"
    temporary = tmp_path / ".candidate.tmp"
    target.write_bytes(baseline)
    temporary.write_bytes(b"candidate\n")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        exchange_verified_baseline(
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


def test_exchange_verified_baseline_restores_raced_target_before_blocking(tmp_path: Path) -> None:
    baseline = b"baseline\n"
    raced = b"changed-after-proof\n"
    target = tmp_path / "automations.yaml"
    temporary = tmp_path / ".candidate.tmp"
    target.write_bytes(raced)
    temporary.write_bytes(b"candidate\n")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(LiveApplyAtomicGuardError, match="baseline changed"):
            exchange_verified_baseline(
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
