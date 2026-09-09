"""Byte-preserving regular-file snapshots, separate from the live configuration."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .errors import Failure
from .main_routing import include_in_main

MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 10000


def valid_path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or len(value.encode()) > 4096
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(part in ("", ".", "..", ".git") for part in parts)
        or PurePosixPath(value).is_absolute()
    ):
        raise Failure("unsafe_path")
    return value


def excluded(name: str) -> bool:
    parts = name.split("/")
    return (
        any(p in (".git", "__pycache__") for p in parts)
        or parts[-1].endswith((".pyc", ".pyo"))
        or not include_in_main(name)
    )


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _scan(root: Path, *, filter_config: bool) -> dict[str, tuple[int, ...]]:
    result = {}

    def walk(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                name = (directory / entry.name).relative_to(root).as_posix()
                if filter_config and excluded(name):
                    continue
                valid_path(name)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    walk(directory / entry.name)
                elif stat.S_ISREG(info.st_mode):
                    result[name] = _fingerprint(info)
                    if len(result) > MAX_FILES:
                        raise Failure("snapshot_too_large")
                else:
                    raise Failure("unsafe_file_type")

    if not stat.S_ISDIR(root.lstat().st_mode):
        raise Failure("unsafe_config_root")
    walk(root)
    return result


@dataclass(frozen=True)
class Snapshot:
    files: Mapping[str, bytes]

    def __post_init__(self) -> None:
        for name, content in self.files.items():
            valid_path(name)
            if any(
                "/".join(name.split("/")[:i]) in self.files for i in range(1, len(name.split("/")))
            ):
                raise Failure("conflicting_paths")
            if not isinstance(content, bytes):
                raise Failure("invalid_file_content")
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))

    @property
    def manifest(self) -> dict[str, str]:
        return {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(self.files.items())
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()

    def save(self, target: Path) -> None:
        if target.exists():
            if self.load(target).digest != self.digest:
                raise Failure("snapshot_corrupt")
            return
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=target.parent))
        try:
            (temporary / "files").mkdir(mode=0o700)
            for name, data in self.files.items():
                path = temporary / "files" / name
                path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with path.open("xb") as out:
                    os.fchmod(out.fileno(), 0o600)
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
            with (temporary / "manifest.json").open("x") as out:
                os.fchmod(out.fileno(), 0o600)
                json.dump({"version": 1, "files": self.manifest}, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            for directory, _, _ in os.walk(temporary, topdown=False):
                fsync_dir(Path(directory))
            os.rename(temporary, target)
            fsync_dir(target.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    @classmethod
    def load(cls, target: Path) -> "Snapshot":
        try:
            if target.is_symlink() or (target / "manifest.json").is_symlink():
                raise Failure("snapshot_corrupt")
            raw = (target / "manifest.json").read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise Failure("snapshot_corrupt")
            manifest = json.loads(raw)
            if manifest["version"] != 1 or not isinstance(manifest["files"], dict):
                raise Failure("snapshot_corrupt")
            snap = capture(target / "files", require_config=False, filter_config=False)
            if snap.manifest != manifest["files"]:
                raise Failure("snapshot_corrupt")
            return snap
        except (OSError, ValueError, KeyError, TypeError, Failure):
            raise Failure("snapshot_corrupt") from None


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def capture(
    root: Path,
    *,
    max_bytes: int = MAX_BYTES,
    require_config: bool = True,
    filter_config: bool = True,
) -> Snapshot:
    try:
        before = _scan(root, filter_config=filter_config)
        files = {}
        total = 0
        for name, fingerprint in before.items():
            fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or _fingerprint(info) != fingerprint:
                    raise Failure("snapshot_changed", retryable=True)
                data = source.read(max_bytes - total + 1)
                total += len(data)
                if total > max_bytes:
                    raise Failure("snapshot_too_large")
                if _fingerprint(os.fstat(source.fileno())) != fingerprint:
                    raise Failure("snapshot_changed", retryable=True)
                files[name] = data
        if before != _scan(root, filter_config=filter_config):
            raise Failure("snapshot_changed", retryable=True)
        if require_config and "configuration.yaml" not in files:
            raise Failure("configuration_missing")
        return Snapshot(files)
    except OSError:
        raise Failure("snapshot_unavailable", retryable=True) from None
