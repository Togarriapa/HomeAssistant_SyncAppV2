from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal, TypeAlias

_CHUNK_SIZE: Final = 1024 * 1024
Route = Literal["main", "logs"]
FileFingerprint: TypeAlias = tuple[int, int, int, int, int, int, int]


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

    Traversal is rooted in directory file descriptors so a path cannot be swapped
    to a symlink between inspection and descent. The entire source tree is scanned
    again after copying and must have the same identity/metadata before the staged
    snapshot is accepted.
    """

    source, root_descriptor, root_expected = _open_source_root(source_root)
    try:
        stage = _validate_staging_root(source, staging_root)
        normalized_logs = {_normalize_log_path(path) for path in log_paths}

        stage.mkdir(mode=0o700)
        try:
            entries: list[SnapshotEntry] = []
            initial_state: dict[str, FileFingerprint] = {}
            _walk_and_copy(
                root_descriptor,
                "",
                stage,
                normalized_logs,
                entries,
                initial_state,
            )
            _assert_root_unchanged(source, root_descriptor, root_expected)

            final_state: dict[str, FileFingerprint] = {}
            _scan_tree(root_descriptor, "", final_state)
            _assert_root_unchanged(source, root_descriptor, root_expected)
            if initial_state != final_state:
                raise SnapshotError("source tree changed during snapshot")

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
    finally:
        os.close(root_descriptor)


def _open_source_root(path: Path) -> tuple[Path, int, FileFingerprint]:
    try:
        expected = path.lstat()
    except FileNotFoundError as exc:
        raise SnapshotError("source root does not exist") from exc
    if stat.S_ISLNK(expected.st_mode):
        raise SnapshotError("source root must not be a symlink")
    if not stat.S_ISDIR(expected.st_mode):
        raise SnapshotError("source root must be a directory")

    source = path.resolve(strict=True)
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise SnapshotError("unable to open source root safely") from exc

    opened = os.fstat(descriptor)
    if _fingerprint(expected) != _fingerprint(opened):
        os.close(descriptor)
        raise SnapshotError("source root changed while opening")
    return source, descriptor, _fingerprint(opened)


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
    directory_descriptor: int,
    prefix: str,
    stage: Path,
    log_paths: set[str],
    entries: list[SnapshotEntry],
    tree_state: dict[str, FileFingerprint],
) -> None:
    children = _scan_children(directory_descriptor, prefix)
    for child, expected in children:
        relative = _relative_path(prefix, child.name)
        tree_state[relative] = _fingerprint(expected)

        if stat.S_ISLNK(expected.st_mode):
            raise SnapshotError(f"symlink is not allowed in snapshot: {relative}")
        if stat.S_ISDIR(expected.st_mode):
            child_descriptor = _open_child_directory(directory_descriptor, child.name, relative, expected)
            try:
                _walk_and_copy(
                    child_descriptor,
                    relative,
                    stage,
                    log_paths,
                    entries,
                    tree_state,
                )
                if _fingerprint(os.fstat(child_descriptor)) != _fingerprint(expected):
                    raise SnapshotError(f"source tree changed during snapshot: {relative}")
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(expected.st_mode):
            raise SnapshotError(f"special file is not allowed in snapshot: {relative}")
        if expected.st_nlink != 1:
            raise SnapshotError(f"hardlink is not allowed in snapshot: {relative}")

        route: Route = "logs" if relative in log_paths else "main"
        digest, size = _copy_regular_file(
            directory_descriptor,
            child.name,
            relative,
            stage / route / relative,
            expected,
        )
        entries.append(SnapshotEntry(relative, route, size, digest))


def _scan_tree(
    directory_descriptor: int,
    prefix: str,
    tree_state: dict[str, FileFingerprint],
) -> None:
    directory_before = os.fstat(directory_descriptor)
    children = _scan_children(directory_descriptor, prefix)
    for child, expected in children:
        relative = _relative_path(prefix, child.name)
        tree_state[relative] = _fingerprint(expected)

        if stat.S_ISLNK(expected.st_mode):
            raise SnapshotError(f"symlink is not allowed in snapshot: {relative}")
        if stat.S_ISDIR(expected.st_mode):
            child_descriptor = _open_child_directory(directory_descriptor, child.name, relative, expected)
            try:
                _scan_tree(child_descriptor, relative, tree_state)
                if _fingerprint(os.fstat(child_descriptor)) != _fingerprint(expected):
                    raise SnapshotError("source tree changed during snapshot")
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(expected.st_mode):
            raise SnapshotError(f"special file is not allowed in snapshot: {relative}")
        if expected.st_nlink != 1:
            raise SnapshotError(f"hardlink is not allowed in snapshot: {relative}")

    if _fingerprint(os.fstat(directory_descriptor)) != _fingerprint(directory_before):
        raise SnapshotError("source tree changed during snapshot")


def _scan_children(
    directory_descriptor: int, prefix: str
) -> list[tuple[os.DirEntry[str], os.stat_result]]:
    try:
        with os.scandir(directory_descriptor) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        location = prefix or "."
        raise SnapshotError(f"unable to enumerate source tree at: {location}") from exc

    result: list[tuple[os.DirEntry[str], os.stat_result]] = []
    for child in children:
        try:
            expected = child.stat(follow_symlinks=False)
        except OSError as exc:
            relative = _relative_path(prefix, child.name)
            raise SnapshotError(f"unable to inspect source path: {relative}") from exc
        result.append((child, expected))
    return result


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    relative: str,
    expected: os.stat_result,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise SnapshotError(f"unable to open source directory safely: {relative}") from exc
    if _fingerprint(os.fstat(descriptor)) != _fingerprint(expected):
        os.close(descriptor)
        raise SnapshotError(f"source tree changed during snapshot: {relative}")
    return descriptor


def _copy_regular_file(
    directory_descriptor: int,
    name: str,
    relative: str,
    destination: Path,
    expected: os.stat_result,
) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise SnapshotError(f"unable to open source file safely: {relative}") from exc

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if _fingerprint(expected) != _fingerprint(opened):
            raise SnapshotError(f"source file changed before copy: {relative}")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise SnapshotError(f"unsafe source file encountered: {relative}")

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
            after_path = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise SnapshotError(f"source file changed during copy: {relative}") from exc
        if _fingerprint(opened) != _fingerprint(after_fd) or _fingerprint(opened) != _fingerprint(
            after_path
        ):
            raise SnapshotError(f"source file changed during copy: {relative}")
        if total != opened.st_size:
            raise SnapshotError(f"source file size changed during copy: {relative}")
    finally:
        os.close(descriptor)

    return digest.hexdigest(), total


def _assert_root_unchanged(source: Path, descriptor: int, expected: FileFingerprint) -> None:
    try:
        current_path = source.lstat()
    except OSError as exc:
        raise SnapshotError("source root changed during snapshot") from exc
    if (
        _fingerprint(os.fstat(descriptor)) != expected
        or _fingerprint(current_path) != expected
        or stat.S_ISLNK(current_path.st_mode)
    ):
        raise SnapshotError("source root changed during snapshot")


def _relative_path(prefix: str, name: str) -> str:
    return f"{prefix}/{name}" if prefix else name


def _fingerprint(info: os.stat_result) -> FileFingerprint:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
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
