"""Fail-closed validation for the live read-only Home Assistant source mount."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


class SourceBoundaryError(RuntimeError):
    """The configured Home Assistant source cannot be trusted as read-only."""


@dataclass(frozen=True, slots=True)
class ValidatedSource:
    path: Path
    device: int
    inode: int


def validate_read_only_source(path: Path) -> ValidatedSource:
    """Prove a stable directory identity and read-only filesystem without writing to it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise SourceBoundaryError("Home Assistant source is not a safe directory") from exc

    try:
        first = os.fstat(fd)
        if not stat.S_ISDIR(first.st_mode):
            raise SourceBoundaryError("Home Assistant source is not a directory")
        filesystem = os.fstatvfs(fd)
        if not filesystem.f_flag & os.ST_RDONLY:
            raise SourceBoundaryError("Home Assistant source filesystem is not read-only")

        canonical = path.resolve(strict=True)
        canonical_fd = os.open(
            canonical,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            canonical_info = os.fstat(canonical_fd)
        finally:
            os.close(canonical_fd)

        second = os.fstat(fd)
        identity = (first.st_dev, first.st_ino)
        if identity != (canonical_info.st_dev, canonical_info.st_ino):
            raise SourceBoundaryError("Home Assistant source canonical identity changed")
        if identity != (second.st_dev, second.st_ino):
            raise SourceBoundaryError("Home Assistant source changed during validation")
        return ValidatedSource(canonical, first.st_dev, first.st_ino)
    except OSError as exc:
        raise SourceBoundaryError("Home Assistant source validation failed closed") from exc
    finally:
        os.close(fd)


def revalidate_source(source: ValidatedSource) -> None:
    """Re-prove immutable source identity and read-only status before a later operation."""
    if type(source) is not ValidatedSource:
        raise SourceBoundaryError("Home Assistant source evidence is invalid")
    current = validate_read_only_source(source.path)
    if current.device != source.device or current.inode != source.inode:
        raise SourceBoundaryError("Home Assistant source identity no longer matches")
