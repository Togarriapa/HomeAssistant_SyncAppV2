"""Baseline-bound atomic removal primitive for live Apply files."""

from __future__ import annotations

import ctypes
import errno
import os
import uuid

from .live_apply_atomic_replace import verify_displaced_leaf

_RENAME_NOREPLACE = 1


class LiveApplyAtomicDeleteError(RuntimeError):
    """A live leaf could not be removed while preserving fail-closed semantics."""


class LiveApplyAtomicDeleteBaselineMismatch(LiveApplyAtomicDeleteError):
    """The leaf moved at the mutation boundary was not the authorized baseline."""


class LiveApplyAtomicDeleteOutcomeUncertain(LiveApplyAtomicDeleteError):
    """A mismatched moved leaf could not be safely restored."""


def commit_verified_deleted_leaf(
    parent_fd: int,
    target: str,
    *,
    expected_object_id: str,
    expected_mode: str,
) -> None:
    """Remove ``target`` only when the atomically displaced leaf is the baseline.

    The target is first atomically renamed to an unpredictable tombstone in the
    already-verified parent directory. Verification therefore occurs against the
    exact object removed from the live name. A mismatch is restored with
    ``RENAME_NOREPLACE`` so an independently-created replacement is never
    overwritten. Failure to restore is explicitly uncertain rather than being
    misclassified as a deterministic pre-mutation block.
    """
    if type(parent_fd) is not int or parent_fd < 0 or not _safe_leaf(target):
        raise LiveApplyAtomicDeleteError("atomic delete arguments are invalid")
    if expected_mode not in {"100644", "100755"}:
        raise LiveApplyAtomicDeleteError("atomic delete baseline mode is invalid")
    if not isinstance(expected_object_id, str) or len(expected_object_id) not in {40, 64}:
        raise LiveApplyAtomicDeleteError("atomic delete object id is invalid")

    tombstone = f".syncapp-delete-{uuid.uuid4().hex}.tmp"
    try:
        _rename_noreplace(parent_fd, target, tombstone)
    except FileNotFoundError as error:
        raise LiveApplyAtomicDeleteBaselineMismatch(
            "live baseline changed before atomic delete"
        ) from error

    if verify_displaced_leaf(
        parent_fd,
        tombstone,
        expected_object_id=expected_object_id,
        expected_mode=expected_mode,
    ):
        try:
            os.unlink(tombstone, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as error:
            raise LiveApplyAtomicDeleteOutcomeUncertain(
                "verified delete tombstone cleanup failed"
            ) from error
        return
    try:
        _rename_noreplace(parent_fd, tombstone, target)
        os.fsync(parent_fd)
    except OSError as error:
        raise LiveApplyAtomicDeleteOutcomeUncertain(
            "live baseline changed and atomic delete restoration is uncertain"
        ) from error
    raise LiveApplyAtomicDeleteBaselineMismatch(
        "live baseline changed; atomic delete was reversed"
    )


def _rename_noreplace(parent_fd: int, source: str, target: str) -> None:
    if not _safe_leaf(source) or not _safe_leaf(target) or source == target:
        raise LiveApplyAtomicDeleteError("atomic delete leaf name is invalid")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise LiveApplyAtomicDeleteError("atomic no-replace rename is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise LiveApplyAtomicDeleteError("atomic no-replace rename is unavailable")
    raise OSError(error, os.strerror(error))


def _safe_leaf(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\x00" not in value
