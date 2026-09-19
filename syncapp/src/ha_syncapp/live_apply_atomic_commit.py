"""Durable commit helper for a verified modified live Apply leaf."""

from __future__ import annotations

import os

from .live_apply_atomic_guard import exchange_verified_baseline


def commit_verified_modified_leaf(
    parent_fd: int,
    temporary: str,
    target: str,
    *,
    expected_object_id: str,
    expected_mode: str,
) -> None:
    """Atomically commit one prepared candidate over its exact authorized baseline.

    On success the displaced, verified baseline is removed and the containing
    directory is fsynced. A deterministic baseline mismatch is reversed by the
    guard and deliberately leaves the prepared candidate available to its caller
    for cleanup. Uncertain reversal outcomes propagate unchanged.
    """
    exchange_verified_baseline(
        parent_fd,
        temporary,
        target,
        expected_object_id=expected_object_id,
        expected_mode=expected_mode,
    )
    os.unlink(temporary, dir_fd=parent_fd)
    os.fsync(parent_fd)
