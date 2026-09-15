"""Linux atomic exchange primitives for race-safe live Apply file replacement."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from typing import NoReturn

_RENAME_EXCHANGE = 2


class LiveApplyAtomicReplaceError(RuntimeError):
    """The atomic leaf exchange could not be completed safely."""


def exchange_leaf(parent_fd: int, temporary: str, target: str) -> None:
    """Atomically exchange two names in one already-verified parent directory.

    After success, ``target`` names the prepared candidate and ``temporary`` names
    the exact object displaced from ``target``. The caller must verify that
    displaced object before deciding whether to keep the exchange or exchange
    the names back.
    """
    if type(parent_fd) is not int or parent_fd < 0:
        _reject("parent directory descriptor is invalid")
    if not _safe_leaf(temporary) or not _safe_leaf(target) or temporary == target:
        _reject("atomic exchange leaf name is invalid")

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        _reject("atomic exchange is unavailable on this platform")
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
        os.fsencode(temporary),
        parent_fd,
        os.fsencode(target),
        _RENAME_EXCHANGE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {38, 22, 95}:  # ENOSYS, EINVAL, EOPNOTSUPP
        _reject("atomic exchange is unavailable on this filesystem")
    raise OSError(error, os.strerror(error))


def exchange_verified_baseline(
    parent_fd: int,
    temporary: str,
    target: str,
    *,
    expected_object_id: str,
    expected_mode: str,
) -> bool:
    """Exchange a candidate only when the displaced target is the exact baseline.

    ``False`` means the displaced object did not match and the original names were
    restored. If restoration cannot be completed, raise rather than claiming a
    safe deterministic block because the live mutation outcome is then uncertain.
    """
    exchange_leaf(parent_fd, temporary, target)
    if verify_displaced_leaf(
        parent_fd,
        temporary,
        expected_object_id=expected_object_id,
        expected_mode=expected_mode,
    ):
        return True
    try:
        exchange_leaf(parent_fd, temporary, target)
    except (LiveApplyAtomicReplaceError, OSError) as exc:
        raise LiveApplyAtomicReplaceError(
            "atomic exchange baseline mismatch could not be safely reversed; live mutation outcome is uncertain"
        ) from exc
    return False


def verify_displaced_leaf(
    parent_fd: int,
    displaced: str,
    *,
    expected_object_id: str,
    expected_mode: str,
) -> bool:
    """Verify the exact regular file displaced by an exchange, without following links.

    This verifier is intentionally read-only. A mismatch returns ``False`` so the
    caller can exchange the names back before classifying the operation as blocked.
    Unsafe arguments or an unavailable displaced object fail closed with ``False``.
    """
    if type(parent_fd) is not int or parent_fd < 0 or not _safe_leaf(displaced):
        return False
    if expected_mode not in {"100644", "100755"}:
        return False
    if not isinstance(expected_object_id, str) or len(expected_object_id) not in {40, 64}:
        return False
    try:
        fd = os.open(displaced, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return False
        data = _read_all(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return (
        _git_mode(info.st_mode) == expected_mode
        and _git_blob_object_id(data, len(expected_object_id)) == expected_object_id
    )


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
    if width == 64:
        return hashlib.sha256(payload).hexdigest()
    return ""


def _git_mode(mode: int) -> str:
    return "100755" if mode & 0o111 else "100644"


def _safe_leaf(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\x00" not in value


def _reject(message: str) -> NoReturn:
    raise LiveApplyAtomicReplaceError(message) from None
