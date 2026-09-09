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
type FileFingerprint = tuple[int, int, int, int, int, int, int]


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


def verify_snapshot(staging_root: Path, manifest: SnapshotManifest) -> None:
    """Verify that isolated staging still matches an accepted snapshot exactly.

    This check is intended to run immediately before any later consumer (for
    example Git publication) uses staged bytes. It detects content, route, path,
    directory, and manifest tampering and never follows links while traversing.
    """

    try:
        expected_files, expected_directories = _validate_manifest(manifest)
        stage, stage_descriptor, stage_expected = _open_directory_root(
            staging_root, "staging root"
        )
        try:
            actual_files: dict[tuple[Route, str], tuple[int, str]] = {}
            actual_directories: set[tuple[Route, str]] = set()
            expected_routes = {entry.route for entry in manifest.entries}
            top_children = _scan_children(stage_descriptor, "")
            actual_routes = {child.name for child, _ in top_children}
            if actual_routes != expected_routes:
                raise SnapshotError("staging routes do not match manifest")

            for child, info in top_children:
                if child.name not in {"main", "logs"} or not stat.S_ISDIR(info.st_mode):
                    raise SnapshotError("invalid staging route")
                route: Route = "main" if child.name == "main" else "logs"
                route_descriptor = _open_child_directory(
                    stage_descriptor, child.name, child.name, info
                )
                try:
                    _verify_staged_tree(
                        route_descriptor,
                        "",
                        route,
                        actual_files,
                        actual_directories,
                    )
                    if _fingerprint(os.fstat(route_descriptor)) != _fingerprint(info):
                        raise SnapshotError("staging tree changed during verification")
                finally:
                    os.close(route_descriptor)

            _assert_directory_root_unchanged(
                stage, stage_descriptor, stage_expected, "staging root"
            )
            if actual_files != expected_files or actual_directories != expected_directories:
                raise SnapshotError("staging contents do not match manifest")
        finally:
            os.close(stage_descriptor)
    except SnapshotError as exc:
        raise SnapshotError("staged snapshot integrity verification failed") from exc


def _validate_manifest(
    manifest: SnapshotManifest,
) -> tuple[
    dict[tuple[Route, str], tuple[int, str]],
    set[tuple[Route, str]],
]:
    entries = manifest.entries
    if entries != tuple(sorted(entries, key=lambda entry: (entry.path, entry.route))):
        raise SnapshotError("snapshot manifest entries are not canonical")
    if manifest.total_bytes != sum(entry.size for entry in entries):
        raise SnapshotError("snapshot manifest byte total is invalid")
    if manifest.snapshot_id != _snapshot_id(entries):
        raise SnapshotError("snapshot manifest identity is invalid")

    files: dict[tuple[Route, str], tuple[int, str]] = {}
    directories: set[tuple[Route, str]] = set()
    source_paths: set[str] = set()
    for entry in entries:
        if entry.route not in {"main", "logs"}:
            raise SnapshotError("snapshot manifest route is invalid")
        normalized = _normalize_relative_path(entry.path, "snapshot path")
        if normalized in source_paths:
            raise SnapshotError("snapshot manifest contains duplicate source path")
        source_paths.add(normalized)
        if entry.size < 0 or len(entry.sha256) != 64:
            raise SnapshotError("snapshot manifest file metadata is invalid")
        try:
            int(entry.sha256, 16)
        except ValueError as exc:
            raise SnapshotError("snapshot manifest digest is invalid") from exc
        files[(entry.route, normalized)] = (entry.size, entry.sha256)

        parts = PurePosixPath(normalized).parts
        for index in range(1, len(parts)):
            directories.add((entry.route, PurePosixPath(*parts[:index]).as_posix()))
    return files, directories


def _verify_staged_tree(
    directory_descriptor: int,
    prefix: str,
    route: Route,
    files: dict[tuple[Route, str], tuple[int, str]],
    directories: set[tuple[Route, str]],
) -> None:
    directory_before = os.fstat(directory_descriptor)
    for child, expected in _scan_children(directory_descriptor, prefix):
        relative = _relative_path(prefix, child.name)
        if stat.S_ISLNK(expected.st_mode):
            raise SnapshotError(f"symlink found in staging: {relative}")
        if stat.S_ISDIR(expected.st_mode):
            directories.add((route, relative))
            child_descriptor = _open_child_directory(
                directory_descriptor, child.name, relative, expected
            )
            try:
                _verify_staged_tree(
                    child_descriptor,
                    relative,
                    route,
                    files,
                    directories,
                )
                if _fingerprint(os.fstat(child_descriptor)) != _fingerprint(expected):
                    raise SnapshotError("staging tree changed during verification")
            finally:
                os.close(child_descriptor)
            continue
        if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
            raise SnapshotError(f"unsafe file found in staging: {relative}")
        size, digest = _hash_regular_file(
            directory_descriptor,
            child.name,
            relative,
            expected,
        )
        files[(route, relative)] = (size, digest)

    if _fingerprint(os.fstat(directory_descriptor)) != _fingerprint(directory_before):
        raise SnapshotError("staging tree changed during verification")


def _hash_regular_file(
    directory_descriptor: int,
    name: str,
    relative: str,
    expected: os.stat_result,
) -> tuple[int, str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise SnapshotError(f"unable to open staged file safely: {relative}") from exc

    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if _fingerprint(opened) != _fingerprint(expected):
            raise SnapshotError("staging tree changed during verification")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise SnapshotError(f"unsafe staged file encountered: {relative}")
        while True:
            chunk = os.read(descriptor, _CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if _fingerprint(opened) != _fingerprint(after) or _fingerprint(opened) != _fingerprint(
            current
        ):
            raise SnapshotError("staging tree changed during verification")
        if total != opened.st_size:
            raise SnapshotError("staged file size changed during verification")
    finally:
        os.close(descriptor)
    return total, digest.hexdigest()


def _open_source_root(path: Path) -> tuple[Path, int, FileFingerprint]:
    return _open_directory_root(path, "source root")


def _open_directory_root(path: Path, label: str) -> tuple[Path, int, FileFingerprint]:
    try:
        expected = path.lstat()
    except FileNotFoundError as exc:
        raise SnapshotError(f"{label} does not exist") from exc
    if stat.S_ISLNK(expected.st_mode):
        raise SnapshotError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(expected.st_mode):
        raise SnapshotError(f"{label} must be a directory")

    resolved = path.resolve(strict=True)
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise SnapshotError(f"unable to open {label} safely") from exc

    opened = os.fstat(descriptor)
    if _fingerprint(expected) != _fingerprint(opened):
        os.close(descriptor)
        raise SnapshotError(f"{label} changed while opening")
    return resolved, descriptor, _fingerprint(opened)


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
    return _normalize_relative_path(path, "log path")


def _normalize_relative_path(path: str, label: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "." in candidate.parts
        or str(candidate) != path
    ):
        raise SnapshotError(f"invalid {label}: {path!r}")
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
            child_descriptor = _open_child_directory(
                directory_descriptor, child.name, relative, expected
            )
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
            child_descriptor = _open_child_directory(
                directory_descriptor, child.name, relative, expected
            )
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
        raise SnapshotError(f"unable to enumerate tree at: {location}") from exc

    result: list[tuple[os.DirEntry[str], os.stat_result]] = []
    for child in children:
        try:
            expected = child.stat(follow_symlinks=False)
        except OSError as exc:
            relative = _relative_path(prefix, child.name)
            raise SnapshotError(f"unable to inspect path: {relative}") from exc
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
        raise SnapshotError(f"unable to open directory safely: {relative}") from exc
    if _fingerprint(os.fstat(descriptor)) != _fingerprint(expected):
        os.close(descriptor)
        raise SnapshotError(f"tree changed while opening directory: {relative}")
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
    _assert_directory_root_unchanged(source, descriptor, expected, "source root")


def _assert_directory_root_unchanged(
    root: Path,
    descriptor: int,
    expected: FileFingerprint,
    label: str,
) -> None:
    try:
        current_path = root.lstat()
    except OSError as exc:
        raise SnapshotError(f"{label} changed during snapshot") from exc
    if (
        _fingerprint(os.fstat(descriptor)) != expected
        or _fingerprint(current_path) != expected
        or stat.S_ISLNK(current_path.st_mode)
    ):
        raise SnapshotError(f"{label} changed during snapshot")


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
