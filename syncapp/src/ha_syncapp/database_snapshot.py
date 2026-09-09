"""Consistent SQLite Recorder snapshots into isolated protected staging."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

_COPY_CHUNK_SIZE = 1024 * 1024


class DatabaseSnapshotError(RuntimeError):
    """A Recorder database snapshot could not be produced safely."""


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    """Integrity evidence for one consistent staged SQLite database."""

    root: Path
    database_path: Path
    size: int
    sha256: str


def capture_sqlite_snapshot(source: Path, staging_root: Path) -> DatabaseSnapshot:
    """Use SQLite online backup to capture committed source state consistently."""
    source_fd = _open_unique_regular_file(source)
    try:
        source_identity = _file_identity(os.fstat(source_fd))
        canonical_source = source.resolve(strict=True)
        _verify_path_identity(source, source_identity)
        staging = _trusted_directory(staging_root, "database staging")
        source_root = canonical_source.parent
        if staging == source_root or source_root in staging.parents or staging in source_root.parents:
            raise DatabaseSnapshotError("database staging overlaps the source tree")

        work_root = staging / f".snapshot-database-{uuid.uuid4().hex}.tmp"
        destination = work_root / source.name
        try:
            work_root.mkdir(mode=0o700)
            _backup_sqlite(canonical_source, destination)
            _verify_path_identity(source, source_identity)
            _fsync_regular_file(destination)
            size, digest = _hash_stable_regular_file(destination)
            return DatabaseSnapshot(work_root, destination, size, digest)
        except Exception as exc:
            shutil.rmtree(work_root, ignore_errors=True)
            if isinstance(exc, DatabaseSnapshotError):
                raise
            raise DatabaseSnapshotError("database snapshot failed closed") from exc
    finally:
        os.close(source_fd)


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"{source.as_uri()}?mode=ro"
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(source_uri, uri=True, timeout=5.0)
        source_connection.execute("PRAGMA query_only = ON")
        destination_connection = sqlite3.connect(destination)
        source_connection.backup(destination_connection, pages=256, sleep=0.01)
        check = destination_connection.execute("PRAGMA quick_check").fetchall()
        if check != [("ok",)]:
            raise DatabaseSnapshotError("staged database failed integrity verification")
        destination_connection.commit()
    except sqlite3.Error as exc:
        raise DatabaseSnapshotError("SQLite backup failed closed") from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def _open_unique_regular_file(path: Path) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DatabaseSnapshotError("Recorder database is not a safe regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise DatabaseSnapshotError("Recorder database is not a unique regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _verify_path_identity(path: Path, expected: tuple[int, int]) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DatabaseSnapshotError("Recorder database identity cannot be revalidated") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or _file_identity(current) != expected
    ):
        raise DatabaseSnapshotError("Recorder database identity changed")


def _trusted_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DatabaseSnapshotError(f"{label} is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise DatabaseSnapshotError(f"{label} must be a real directory")
    return path.resolve(strict=True)


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _fsync_regular_file(path: Path) -> None:
    descriptor = _open_unique_regular_file(path)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise DatabaseSnapshotError("staged database could not be synchronized") from exc
    finally:
        os.close(descriptor)


def _hash_stable_regular_file(path: Path) -> tuple[int, str]:
    descriptor = _open_unique_regular_file(path)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise DatabaseSnapshotError("staged database changed during hashing")
        return after.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)
