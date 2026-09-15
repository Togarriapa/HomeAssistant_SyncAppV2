"""Linux atomic exchange primitive for race-safe live Apply file replacement."""

from __future__ import annotations

import ctypes
import errno
import os
from typing import NoReturn

_RENAME_EXCHANGE = 2


class LiveApplyAtomicReplaceError(RuntimeError):
    """The atomic leaf exchange could not be completed safely."""


def exchange_leaf(parent_fd: int, temporary: str, target: str) -> None:
    """Atomically exchange two names in one already-verified parent directory.

    After success, ``target`` names the prepared candidate and ``temporary`` names
    the exact object displaced from ``target``.  The caller must verify that
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
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
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
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        _reject("atomic exchange is unavailable on this filesystem")
    raise OSError(error, os.strerror(error))


def _safe_leaf(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\x00" not in value


def _reject(message: str) -> NoReturn:
    raise LiveApplyAtomicReplaceError(message) from None
