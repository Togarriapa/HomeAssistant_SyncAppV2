"""Fail-closed metadata observation for routine Local configuration changes."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .main_routing import include_in_main


class LocalChangeSourceError(RuntimeError):
    """The Home Assistant source cannot be observed safely."""


@dataclass(frozen=True, slots=True)
class LocalChangeEntry:
    """Bounded metadata evidence for one Repo B main source file."""

    path: str
    device: int
    inode: int
    mode: int
    links: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class LocalChangeSnapshot:
    """One deterministic metadata view of the configuration source."""

    device: int
    inode: int
    entries: tuple[LocalChangeEntry, ...]


def observe_local_change_source(source: Path) -> LocalChangeSnapshot:
    """Observe Repo B main-routed source metadata without reading or mutating file contents."""
    try:
        root_metadata = source.lstat()
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
            raise LocalChangeSourceError("local change source is not a real directory")
        canonical = source.resolve(strict=True)
        canonical_metadata = canonical.stat(follow_symlinks=False)
        root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        if root_identity != (canonical_metadata.st_dev, canonical_metadata.st_ino):
            raise LocalChangeSourceError("local change source identity is inconsistent")

        entries: list[LocalChangeEntry] = []

        def walk(directory: Path, relative: Path) -> None:
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise LocalChangeSourceError(
                    "local change source cannot be scanned safely"
                ) from exc

            for child in children:
                child_relative = relative / child.name
                relative_path = child_relative.as_posix()
                try:
                    metadata = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise LocalChangeSourceError("local change source changed during scan") from exc

                if stat.S_ISDIR(metadata.st_mode):
                    if stat.S_ISLNK(metadata.st_mode):
                        raise LocalChangeSourceError(
                            "local change source contains a directory symlink"
                        )
                    walk(Path(child.path), child_relative)
                    continue

                try:
                    included = include_in_main(relative_path)
                except ValueError as exc:
                    raise LocalChangeSourceError("local change source path routing failed") from exc
                if not included:
                    continue

                if stat.S_ISLNK(metadata.st_mode):
                    raise LocalChangeSourceError("local change source contains a symbolic link")
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise LocalChangeSourceError("local change source contains an unsafe file")

                entries.append(
                    LocalChangeEntry(
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

        walk(canonical, Path())

        final_metadata = canonical.stat(follow_symlinks=False)
        if root_identity != (final_metadata.st_dev, final_metadata.st_ino):
            raise LocalChangeSourceError("local change source changed during scan")
        return LocalChangeSnapshot(root_metadata.st_dev, root_metadata.st_ino, tuple(entries))
    except LocalChangeSourceError:
        raise
    except OSError as exc:
        raise LocalChangeSourceError("local change source observation failed closed") from exc
