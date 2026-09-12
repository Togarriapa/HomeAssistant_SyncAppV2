"""Linux inotify transport for bounded routine Local configuration change signals."""

from __future__ import annotations

import ctypes
import errno
import os
import select
import stat
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final


class LocalChangeInotifyError(RuntimeError):
    """The Linux local change event transport could not operate safely."""


_EVENT_HEADER = struct.Struct("iIII")
_READ_SIZE: Final = 64 * 1024
_STOP_POLL_SECONDS: Final = 0.1

_IN_MODIFY: Final = 0x00000002
_IN_ATTRIB: Final = 0x00000004
_IN_CLOSE_WRITE: Final = 0x00000008
_IN_MOVED_FROM: Final = 0x00000040
_IN_MOVED_TO: Final = 0x00000080
_IN_CREATE: Final = 0x00000100
_IN_DELETE: Final = 0x00000200
_IN_DELETE_SELF: Final = 0x00000400
_IN_MOVE_SELF: Final = 0x00000800
_IN_UNMOUNT: Final = 0x00002000
_IN_Q_OVERFLOW: Final = 0x00004000
_IN_IGNORED: Final = 0x00008000
_IN_ONLYDIR: Final = 0x01000000
_IN_DONT_FOLLOW: Final = 0x02000000

_WATCH_MASK: Final = (
    _IN_MODIFY
    | _IN_ATTRIB
    | _IN_CLOSE_WRITE
    | _IN_MOVED_FROM
    | _IN_MOVED_TO
    | _IN_CREATE
    | _IN_DELETE
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
    | _IN_UNMOUNT
    | _IN_Q_OVERFLOW
    | _IN_IGNORED
    | _IN_ONLYDIR
    | _IN_DONT_FOLLOW
)

InotifyAddWatch = Callable[[int, bytes, int], int]


def consume_local_change_events(
    source: Path,
    notify: Callable[[], None],
    stop: threading.Event,
    ready: Callable[[], None],
) -> int:
    """Forward inotify activity as bounded dirty signals until cooperative shutdown."""
    if not isinstance(source, Path):
        raise LocalChangeInotifyError("local change inotify source is invalid")
    if not callable(notify):
        raise LocalChangeInotifyError("local change inotify notifier is invalid")
    if type(stop) is not threading.Event:
        raise LocalChangeInotifyError("local change inotify stop event is invalid")
    if not callable(ready):
        raise LocalChangeInotifyError("local change inotify readiness notifier is invalid")

    libc = ctypes.CDLL(None, use_errno=True)
    inotify_init1 = libc.inotify_init1
    inotify_init1.argtypes = [ctypes.c_int]
    inotify_init1.restype = ctypes.c_int
    inotify_add_watch = libc.inotify_add_watch
    inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    inotify_add_watch.restype = ctypes.c_int

    fd = inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
    if fd < 0:
        _raise_errno("local change inotify initialization failed")

    watched: dict[Path, int] = {}
    forwarded = 0
    try:
        _refresh_watches(source, fd, watched, inotify_add_watch)
        ready()
        while not stop.is_set():
            try:
                readable, _, _ = select.select([fd], [], [], _STOP_POLL_SECONDS)
            except OSError as exc:
                raise LocalChangeInotifyError("local change inotify wait failed") from exc
            if not readable:
                continue

            try:
                payload = os.read(fd, _READ_SIZE)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise LocalChangeInotifyError("local change inotify read failed") from exc
            if not payload:
                raise LocalChangeInotifyError("local change inotify closed unexpectedly")

            invalidated_watches = _validate_event_payload(payload)
            if invalidated_watches:
                for path, watch in tuple(watched.items()):
                    if watch in invalidated_watches:
                        del watched[path]
            _refresh_watches(source, fd, watched, inotify_add_watch)
            notify()
            forwarded += 1
        return forwarded
    finally:
        os.close(fd)


def _refresh_watches(
    source: Path,
    fd: int,
    watched: dict[Path, int],
    add_watch: InotifyAddWatch,
) -> None:
    directories = _safe_directories(source)
    active = set(directories)
    for path in tuple(watched):
        if path not in active:
            del watched[path]
    for path in directories:
        if path in watched:
            continue
        encoded = os.fsencode(path)
        watch = add_watch(fd, encoded, _WATCH_MASK)
        if watch < 0:
            error_number = _current_errno()
            if error_number == errno.ENOENT and path != source:
                continue
            _raise_errno("local change inotify watch registration failed", error_number)
        watched[path] = watch


def _safe_directories(source: Path) -> tuple[Path, ...]:
    directories: list[Path] = []

    def walk(directory: Path, *, is_root: bool) -> None:
        try:
            metadata = directory.lstat()
        except OSError as exc:
            if not is_root and exc.errno == errno.ENOENT:
                return
            raise LocalChangeInotifyError("local change inotify source is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise LocalChangeInotifyError("local change inotify source is not a real directory")

        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            if not is_root and exc.errno == errno.ENOENT:
                return
            raise LocalChangeInotifyError("local change inotify source cannot be scanned") from exc
        directories.append(directory)

        for entry in entries:
            try:
                entry_metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    continue
                raise LocalChangeInotifyError(
                    "local change inotify source changed during scan"
                ) from exc
            if stat.S_ISLNK(entry_metadata.st_mode):
                raise LocalChangeInotifyError(
                    "local change inotify source contains a symbolic link"
                )
            if stat.S_ISDIR(entry_metadata.st_mode):
                walk(Path(entry.path), is_root=False)

    walk(source, is_root=True)
    return tuple(directories)


def _validate_event_payload(payload: bytes) -> frozenset[int]:
    """Validate one kernel payload and return watches whose path identity was lost."""
    offset = 0
    size = len(payload)
    invalidated: set[int] = set()
    while offset < size:
        if size - offset < _EVENT_HEADER.size:
            raise LocalChangeInotifyError("local change inotify event payload is truncated")
        watch, mask, _, name_length = _EVENT_HEADER.unpack_from(payload, offset)
        if mask & (_IN_DELETE_SELF | _IN_MOVE_SELF | _IN_UNMOUNT | _IN_IGNORED):
            invalidated.add(watch)
        offset += _EVENT_HEADER.size + name_length
        if offset > size:
            raise LocalChangeInotifyError("local change inotify event payload is truncated")
    return frozenset(invalidated)


def _current_errno() -> int:
    error_number = ctypes.get_errno()
    return error_number if error_number != 0 else errno.EIO


def _raise_errno(message: str, error_number: int | None = None) -> None:
    resolved_error = _current_errno() if error_number is None else error_number
    raise LocalChangeInotifyError(message) from OSError(resolved_error, os.strerror(resolved_error))
