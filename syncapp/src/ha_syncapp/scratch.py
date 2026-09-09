"""Protected transient roots for synchronization staging under app-owned state."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


class ScratchError(RuntimeError):
    """Synchronization scratch storage is unsafe or unavailable."""


@dataclass(frozen=True, slots=True)
class SyncScratch:
    snapshots: Path
    workspaces: Path


_ROOT_NAMES = ("snapshots", "workspaces")
_TRANSIENT_PREFIXES = {
    "snapshots": ".snapshot-",
    "workspaces": ".git-workspace-",
}


def prepare_sync_scratch(app_state_root: Path) -> SyncScratch:
    """Create owner-only staging roots immediately below protected app state."""
    root_fd = _open_owned_directory(app_state_root)
    try:
        for name in _ROOT_NAMES:
            _ensure_private_child(root_fd, name)
        os.fsync(root_fd)
    except OSError as exc:
        raise ScratchError("synchronization scratch could not be prepared") from exc
    finally:
        os.close(root_fd)
    return SyncScratch(
        snapshots=app_state_root / "snapshots",
        workspaces=app_state_root / "workspaces",
    )


def cleanup_stale_sync_scratch(app_state_root: Path) -> int:
    """Remove only recognized transient children from dedicated scratch roots."""
    root_fd = _open_owned_directory(app_state_root)
    removed = 0
    try:
        for name in _ROOT_NAMES:
            child_fd = _open_private_child(root_fd, name)
            try:
                prefix = _TRANSIENT_PREFIXES[name]
                for entry in os.listdir(child_fd):
                    if not entry.startswith(prefix) or not entry.endswith(".tmp"):
                        continue
                    _remove_owned_tree(child_fd, entry)
                    removed += 1
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
        os.fsync(root_fd)
    except OSError as exc:
        raise ScratchError("synchronization scratch cleanup failed closed") from exc
    finally:
        os.close(root_fd)
    return removed


def _open_owned_directory(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ScratchError("app state root is not a safe directory") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ScratchError("app state root has unsafe ownership or type")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _ensure_private_child(root_fd: int, name: str) -> None:
    with suppress(FileExistsError):
        os.mkdir(name, mode=0o700, dir_fd=root_fd)
    child_fd = _open_private_child(root_fd, name)
    try:
        os.fchmod(child_fd, 0o700)
        os.fsync(child_fd)
    finally:
        os.close(child_fd)


def _open_private_child(root_fd: int, name: str) -> int:
    fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=root_fd,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ScratchError("synchronization scratch directory is unsafe")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _remove_owned_tree(parent_fd: int, name: str) -> None:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ScratchError("stale synchronization scratch entry is unsafe")
    child_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )
    try:
        child_info = os.fstat(child_fd)
        if child_info.st_dev != info.st_dev or child_info.st_ino != info.st_ino:
            raise ScratchError("stale synchronization scratch changed during cleanup")
        for entry in os.listdir(child_fd):
            entry_info = os.stat(entry, dir_fd=child_fd, follow_symlinks=False)
            if entry_info.st_uid != os.geteuid():
                raise ScratchError("stale synchronization scratch has unsafe ownership")
            if stat.S_ISDIR(entry_info.st_mode):
                _remove_owned_tree(child_fd, entry)
            elif stat.S_ISREG(entry_info.st_mode):
                os.unlink(entry, dir_fd=child_fd)
            else:
                raise ScratchError("stale synchronization scratch contains unsafe file type")
        os.fsync(child_fd)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)
