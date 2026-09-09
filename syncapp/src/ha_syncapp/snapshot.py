from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal

_CHUNK_SIZE: Final = 1024 * 1024
Route = Literal["main", "logs"]


class SnapshotError(RuntimeError):
    """Raised when a Home Assistant tree cannot be snapshotted safely."""


@dataclass(frozen=True, order=True)
class SnapshotEntry:
    path: str
    route: Route
    size: int
    sha256: str


@dataclass(frozen=True)
class SnapshotManifest:
    snapshot_id: str
    entries: tuple[SnapshotEntry, ...]
    total_bytes: int


def capture_snapshot(
    source_root: Path,
    staging_root: Path,
    *,
    log_paths: set[str] | frozenset[str] = frozenset(),
) -> SnapshotManifest:
    """Copy a stable, complete Home Assistant tree into isolated branch staging.

    Every regular file is represented exactly once. Paths explicitly supplied in
    ``log_paths`` are routed to ``staging_root/logs``; every other regular file is
    routed to ``staging_root/main``. File classes are never filtered implicitly.

    The staging root must be new and outside the source tree. Unsafe links,
    hardlinks, special files, and files that change while being copied fail closed.
    A failed capture removes the incomplete staging root.
    """

    source = _validate_source_root(source_root)
    stage = _validate_staging_root(source, staging_root)
    normalized_logs = {_normalize_log_path(path) for path in log_paths}

    stage.mkdir(mode=0o700)
    try:
        entries: list[SnapshotEntry] = []
        _walk_and_copy(source, source, stage, normalized_logs, entries)
        entries.sort(key=lambda entry: (entry.path, entry.route))
        frozen_entries = tuple(entries)
        return SnapshotManifest(
            snapshot_id=_snapshot_id(frozen_entries),
            entries=frozen_entries,
            total_bytes=sum(entry.size for entry in frozen_entries),
        )
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _validate_source_root(path: Path) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SnapshotError("source root does not exist") from exc
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotError("source root must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotError("source root must be a directory")
    return path.resolve(strict=True)


def _validate_staging_root(source: Path, path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise SnapshotError("staging root must not already exist")
    stage = path.resolve(strict=False)
    if _is_relative_to(stage, source) or _is_relative_to(source, stage):
        raise SnapshotError("staging root must be outside the source tree")
    if not stage.parent.exists():
        raise SnapshotError("staging root parent must already exist")
    parent_info = stage.parent.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise SnapshotError("staging root parent must be a real directory")
    return stage


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True


def _normalize_log_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "." in candidate.parts
        or str(candidate) != path
    ):
        raise SnapshotError(f"invalid log path: {path!r}")
    return candidate.as_posix()


def _walk_and_copy(
    source_root: Path,
    directory: Path,
    stage: Path,
    log_paths: set[str],
    entries: list[SnapshotEntry],
) -> None:
    try:
        children = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError as exc:
        raise SnapshotError("unable to enumerate source tree") from exc

    with _closing_scandir(children):
        for child in children:
            source_path = directory / child.name
            relative = source_path.relative_to(source_root).as_posix()
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise SnapshotError(f"unable to inspect source path: {relative}") from exc

            if stat.S_ISLNK(info.st_mode):
                raise SnapshotError(f"symlink is not allowed in snapshot: {relative}")
            if stat.S_ISDIR(info.st_mode):
                _walk_and_copy(source_root, source_path, stage, log_paths, entries)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise SnapshotError(f"special file is not allowed in snapshot: {relative}")
            if info.st_nlink != 1:
                raise SnapshotError(f"hardlink is not allowed in snapshot: {relative}")

            route: Route = "logs" if relative in log_paths else "main"
            digest, size = _copy_regular_file(source_path, stage / route / relative, info)
            entries.append(SnapshotEntry(relative, route, size, digest))


class _closing_scandir:
    """Compatibility wrapper for a materialized scandir sequence."""

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = entries

    def __enter__(self) -> list[os.DirEntry[str]]:
        return self._entries

    def __exit__(self, *_args: object) -> None:
        return None


def _copy_regular_file(source: Path, destination: Path, expected: os.stat_result) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise SnapshotError(f"unable to open source file safely: {source.name}") from exc

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if not _same_file_version(expected, opened):
            raise SnapshotError(f"source file changed before copy: {source.name}")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise SnapshotError(f"unsafe source file encountered: {source.name}")

        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        output = os.open(destination, destination_flags, 0o600)
        try:
            while True:
                chunk = os.read(descriptor, _CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                _write_all(output, chunk)
                total += len(chunk)
            os.fsync(output)
        finally:
            os.close(output)

        after_fd = os.fstat(descriptor)
        try:
            after_path = source.stat(follow_symlinks=False)
        except OSError as exc:
            raise SnapshotError(f"source file changed during copy: {source.name}") from exc
        if not _same_file_version(opened, after_fd) or not _same_file_version(opened, after_path):
            raise SnapshotError(f"source file changed during copy: {source.name}")
        if total != opened.st_size:
            raise SnapshotError(f"source file size changed during copy: {source.name}")
    finally:
        os.close(descriptor)

    return digest.hexdigest(), total


def _same_file_version(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise SnapshotError("unable to write staged snapshot")
        offset += written


def _snapshot_id(entries: tuple[SnapshotEntry, ...]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry.route.encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
