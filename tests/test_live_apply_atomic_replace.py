from __future__ import annotations

import os
from pathlib import Path

import pytest

from ha_syncapp.live_apply_atomic_replace import LiveApplyAtomicReplaceError, exchange_leaf


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


def test_exchange_leaf_rejects_path_components(tmp_path: Path) -> None:
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(LiveApplyAtomicReplaceError, match="leaf name is invalid"):
            exchange_leaf(parent_fd, "nested/candidate", "automations.yaml")
    finally:
        os.close(parent_fd)
