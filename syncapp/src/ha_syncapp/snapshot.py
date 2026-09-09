from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_MANIFEST_VERSION = 1
_COPY_CHUNK_SIZE = 1024 * 1024


class SnapshotError(RuntimeError):
    """Raised when a source or staged snapshot cannot be trusted."""


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: str
    size: int
    mode: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_id: str
    root: Path
    tree_path: Path
    manifest_path: Path
    files: tuple[SnapshotFile, ...]


@dataclass(frozen=True, slots=True)
class _SourceFile:
    path: str
    device: int
    inode: int
    mode: int
    links: int
    size: int
    mtime_ns: int
    ctime_ns: int


def capture_snapshot(
    source: Path,
    staging_root: Path,
    *,
    include_path: Callable[[str], bool] | None = None,
) -> Snapshot:
    """Capture a stable selected source tree into isolated staging and bind a manifest."""
    source = _trusted_directory(source, "source")
    staging_root = _trusted_directory(staging_root, "staging")
    _reject_overlap(source, staging_root)

    initial = _scan_source(source, include_path)
    work_root = staging_root / f".snapshot-{uuid.uuid4().hex}.tmp"
    tree_path = work_root / "tree"
    try:
        tree_path.mkdir(parents=True, mode=0o700)
        files: list[SnapshotFile] = []
        for entry in initial:
            destination = tree_path / Path(entry.path)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = _copy_regular_file(source / Path(entry.path), destination)
            copied = destination.stat(follow_symlinks=False)
            files.append(
                SnapshotFile(
                    path=entry.path,
                    size=copied.st_size,
                    mode=stat.S_IMODE(copied.st_mode),
                    sha256=digest,
                )
            )

        final = _scan_source(source, include_path)
        if initial != final:
            raise SnapshotError("source tree changed while snapshot was being captured")

        normalized = tuple(sorted(files, key=lambda item: item.path))
        snapshot_id = _snapshot_id(normalized)
        manifest_path = work_root / "manifest.json"
        manifest_path.write_bytes(_manifest_bytes(snapshot_id, normalized))
        os.chmod(manifest_path, 0o600)

        snapshot = Snapshot(snapshot_id, work_root, tree_path, manifest_path, normalized)
        return verify_snapshot(snapshot.root)
    except Exception as exc:
        shutil.rmtree(work_root, ignore_errors=True)
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError("snapshot capture failed") from exc


def verify_snapshot(snapshot_root: Path) -> Snapshot:
    """Re-verify staged bytes and manifest evidence before a later consumer uses them."""
    snapshot_root = _trusted_directory(snapshot_root, "snapshot")
    manifest_path = snapshot_root / "manifest.json"
    tree_path = snapshot_root / "tree"
    if not tree_path.is_dir() or tree_path.is_symlink():
        raise SnapshotError("snapshot tree is missing or unsafe")

    manifest = _read_manifest(manifest_path)
    expected = _manifest_files(manifest)
    actual_source = _scan_source(tree_path)
    actual_paths = tuple(item.path for item in actual_source)
    expected_paths = tuple(item.path for item in expected)
    if actual_paths != expected_paths:
        raise SnapshotError("staged snapshot paths do not match manifest")

    actual: list[SnapshotFile] = []
    for expected_file in expected:
        path = tree_path / Path(expected_file.path)
        digest = _hash_regular_file(path)
        metadata = path.stat(follow_symlinks=False)
        actual_file = SnapshotFile(
            path=expected_file.path,
            size=metadata.st_size,
            mode=stat.S_IMODE(metadata.st_mode),
            sha256=digest,
        )
        if actual_file != expected_file:
            raise SnapshotError("staged snapshot content does not match manifest")
        actual.append(actual_file)

    files = tuple(actual)
    snapshot_id = _snapshot_id(files)
    declared_id = manifest.get("snapshot_id")
    if not isinstance(declared_id, str) or declared_id != snapshot_id:
        raise SnapshotError("snapshot manifest identity is invalid")
    if _manifest_bytes(snapshot_id, files) != manifest_path.read_bytes():
        raise SnapshotError("snapshot manifest is not canonical")
    return Snapshot(snapshot_id, snapshot_root, tree_path, manifest_path, files)


def _trusted_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SnapshotError(f"{label} directory is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise SnapshotError(f"{label} path must be a real directory")
    return path.resolve(strict=True)


def _reject_overlap(source: Path, staging: Path) -> None:
    if source == staging or source in staging.parents or staging in source.parents:
        raise SnapshotError("source and staging directories must not overlap")


def _scan_source(
    root: Path,
    include_path: Callable[[str], bool] | None = None,
) -> tuple[_SourceFile, ...]:
    files: list[_SourceFile] = []

    def walk(directory: Path, relative: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise SnapshotError("source tree cannot be scanned safely") from exc
        for entry in entries:
            child_relative = relative / entry.name
            relative_path = child_relative.as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SnapshotError("source entry changed during scan") from exc
            if stat.S_ISDIR(metadata.st_mode):
                walk(Path(entry.path), child_relative)
                continue
            if include_path is not None:
                try:
                    included = include_path(relative_path)
                except Exception as exc:
                    raise SnapshotError("snapshot path selection failed") from exc
                if type(included) is not bool:
                    raise SnapshotError("snapshot path selection returned an invalid result")
                if not included:
                    continue
            if stat.S_ISLNK(metadata.st_mode):
                raise SnapshotError("symbolic links are not accepted in snapshots")
            if not stat.S_ISREG(metadata.st_mode):
                raise SnapshotError("special files are not accepted in snapshots")
            if metadata.st_nlink != 1:
                raise SnapshotError("hard-linked files are not accepted in snapshots")
            files.append(
                _SourceFile(
                    path=relative_path,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    mode=metadata.st_mode,
                    links=metadata.st_nlink,
                    size=metadata.st_size,
                    mtime_ns=metadata.st_mtime_ns,
                    ctime_ns=metadata.st_ctime_ns,
                )
            )

    walk(root, Path())
    return tuple(files)


def _copy_regular_file(source: Path, destination: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise SnapshotError("source file cannot be opened safely") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        _validate_regular_metadata(before)
        with destination.open("xb") as target:
            while True:
                chunk = os.read(descriptor, _COPY_CHUNK_SIZE)
                if not chunk:
                    break
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(descriptor)
        if _metadata_identity(before) != _metadata_identity(after):
            raise SnapshotError("source file changed while it was copied")
        os.chmod(destination, stat.S_IMODE(before.st_mode))
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _hash_regular_file(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SnapshotError("staged file cannot be opened safely") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        _validate_regular_metadata(before)
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _metadata_identity(before) != _metadata_identity(after):
            raise SnapshotError("staged file changed while it was verified")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _validate_regular_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SnapshotError("snapshot file is not a unique regular file")


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _snapshot_id(files: tuple[SnapshotFile, ...]) -> str:
    payload = {
        "version": _MANIFEST_VERSION,
        "files": [_file_dict(item) for item in files],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _manifest_bytes(snapshot_id: str, files: tuple[SnapshotFile, ...]) -> bytes:
    payload = {
        "version": _MANIFEST_VERSION,
        "snapshot_id": snapshot_id,
        "files": [_file_dict(item) for item in files],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _file_dict(item: SnapshotFile) -> dict[str, str | int]:
    return {
        "path": item.path,
        "size": item.size,
        "mode": item.mode,
        "sha256": item.sha256,
    }


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SnapshotError("snapshot manifest is unsafe")
        raw = path.read_bytes()
        document = json.loads(raw)
    except SnapshotError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("snapshot manifest cannot be read") from exc
    if not isinstance(document, dict) or set(document) != {"version", "snapshot_id", "files"}:
        raise SnapshotError("snapshot manifest has an invalid schema")
    if document.get("version") != _MANIFEST_VERSION:
        raise SnapshotError("snapshot manifest version is unsupported")
    return document


def _manifest_files(manifest: dict[str, Any]) -> tuple[SnapshotFile, ...]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise SnapshotError("snapshot manifest file list is invalid")
    parsed: list[SnapshotFile] = []
    previous = ""
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "mode", "sha256"}:
            raise SnapshotError("snapshot manifest file entry is invalid")
        path = raw.get("path")
        size = raw.get("size")
        mode = raw.get("mode")
        digest = raw.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in Path(path).parts
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or path <= previous
        ):
            raise SnapshotError("snapshot manifest file entry is invalid")
        parsed.append(SnapshotFile(path=path, size=size, mode=mode, sha256=digest))
        previous = path
    return tuple(parsed)
