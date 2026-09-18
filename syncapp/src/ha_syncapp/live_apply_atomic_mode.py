"""Baseline-bound mode mutation primitive for live Apply files."""

from __future__ import annotations

import hashlib
import os
import stat


class LiveApplyAtomicModeError(RuntimeError):
    """A live leaf mode could not be changed with fail-closed semantics."""


class LiveApplyAtomicModeBaselineMismatch(LiveApplyAtomicModeError):
    """The opened leaf was not the authorized baseline before mutation."""


class LiveApplyAtomicModeOutcomeUncertain(LiveApplyAtomicModeError):
    """The live name changed after mode mutation began."""


def commit_verified_mode_leaf(
    parent_fd: int,
    target: str,
    *,
    expected_object_id: str,
    expected_mode: str,
    target_mode: str,
) -> None:
    """Change mode through a descriptor verified as the exact authorized baseline."""
    if type(parent_fd) is not int or parent_fd < 0 or not _safe_leaf(target):
        raise LiveApplyAtomicModeError("atomic mode arguments are invalid")
    if expected_mode not in {"100644", "100755"} or target_mode not in {
        "100644",
        "100755",
    }:
        raise LiveApplyAtomicModeError("atomic mode is invalid")
    if not isinstance(expected_object_id, str) or len(expected_object_id) not in {40, 64}:
        raise LiveApplyAtomicModeError("atomic mode object id is invalid")
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        raise LiveApplyAtomicModeBaselineMismatch(
            "live baseline changed before mode mutation"
        ) from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise LiveApplyAtomicModeBaselineMismatch("live baseline is not a regular file")
        data = _read_all(fd)
        if _git_mode(before.st_mode) != expected_mode or _git_blob_object_id(
            data, len(expected_object_id)
        ) != expected_object_id:
            raise LiveApplyAtomicModeBaselineMismatch(
                "live baseline changed before mode mutation"
            )
        os.fchmod(fd, 0o755 if target_mode == "100755" else 0o644)
        os.fsync(fd)
        try:
            named = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise LiveApplyAtomicModeOutcomeUncertain(
                "live name changed after mode mutation"
            ) from error
        if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
            raise LiveApplyAtomicModeOutcomeUncertain(
                "live name changed after mode mutation"
            )
        os.fsync(parent_fd)
    finally:
        os.close(fd)


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _git_blob_object_id(data: bytes, width: int) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    if width == 40:
        return hashlib.sha1(payload, usedforsecurity=False).hexdigest()
    return hashlib.sha256(payload).hexdigest() if width == 64 else ""


def _git_mode(mode: int) -> str:
    return "100755" if mode & 0o111 else "100644"


def _safe_leaf(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\x00" not in value
