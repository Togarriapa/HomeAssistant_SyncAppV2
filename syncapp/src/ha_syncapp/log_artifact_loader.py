"""Fail-closed reconstruction of exact staged log artifacts for recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from ha_syncapp.log_artifact import (
    LogArtifact,
    LogArtifactError,
    LogArtifactFile,
    verify_log_artifact,
)

_ARTIFACT_ID = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class LogArtifactLoadError(RuntimeError):
    """An existing staged log artifact cannot be reconstructed safely."""


def load_log_artifact(artifact_root: Path, artifact_id: str) -> LogArtifact:
    """Reconstruct and reverify one exact manifest-derived artifact under a protected root."""
    try:
        root_before = _validate_protected_root(artifact_root)
        if type(artifact_id) is not str or _ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise LogArtifactLoadError("log artifact identifier is invalid")

        artifact_path = artifact_root / artifact_id
        manifest = _read_private_manifest(artifact_path)
        if hashlib.sha256(manifest).hexdigest() != artifact_id:
            raise LogArtifactLoadError("log artifact manifest does not match its identifier")

        files = _manifest_file_evidence(manifest)
        files += (
            LogArtifactFile(
                path="manifest.json",
                sha256=artifact_id,
                size=len(manifest),
            ),
        )
        artifact = LogArtifact(root=artifact_path, artifact_id=artifact_id, files=files)
        verify_log_artifact(artifact)

        root_after = _validate_protected_root(artifact_root)
        if _stat_identity(root_before) != _stat_identity(root_after):
            raise LogArtifactLoadError("log artifact root changed during reconstruction")
        return artifact
    except LogArtifactLoadError:
        raise
    except (LogArtifactError, OSError, ValueError, TypeError) as exc:
        raise LogArtifactLoadError("log artifact reconstruction failed closed") from exc


def _manifest_file_evidence(manifest: bytes) -> tuple[LogArtifactFile, ...]:
    try:
        payload = json.loads(manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LogArtifactLoadError("log artifact manifest is invalid") from exc
    if type(payload) is not dict:
        raise LogArtifactLoadError("log artifact manifest is invalid")
    encoded_files = payload.get("files")
    if type(encoded_files) is not list:
        raise LogArtifactLoadError("log artifact manifest file evidence is invalid")

    files: list[LogArtifactFile] = []
    for encoded in encoded_files:
        if type(encoded) is not dict or set(encoded) != {"path", "sha256", "size"}:
            raise LogArtifactLoadError("log artifact manifest file evidence is invalid")
        path = encoded.get("path")
        digest = encoded.get("sha256")
        size = encoded.get("size")
        if (
            type(path) is not str
            or type(digest) is not str
            or _SHA256.fullmatch(digest) is None
            or type(size) is not int
            or size < 0
            or size > _MAX_MANIFEST_BYTES
        ):
            raise LogArtifactLoadError("log artifact manifest file evidence is invalid")
        files.append(LogArtifactFile(path=path, sha256=digest, size=size))
    return tuple(files)


def _validate_protected_root(root: Path) -> os.stat_result:
    if not isinstance(root, Path) or not root.is_absolute():
        raise LogArtifactLoadError("log artifact protected root must be an absolute path")
    try:
        evidence = root.lstat()
    except OSError as exc:
        raise LogArtifactLoadError("log artifact protected root is unavailable") from exc
    if (
        not stat.S_ISDIR(evidence.st_mode)
        or stat.S_IMODE(evidence.st_mode) != 0o700
        or evidence.st_nlink < 1
    ):
        raise LogArtifactLoadError("log artifact protected root is unsafe")
    return evidence


def _read_private_manifest(artifact_path: Path) -> bytes:
    try:
        artifact_stat = artifact_path.lstat()
    except OSError as exc:
        raise LogArtifactLoadError("log artifact is unavailable") from exc
    if not stat.S_ISDIR(artifact_stat.st_mode) or stat.S_IMODE(artifact_stat.st_mode) != 0o700:
        raise LogArtifactLoadError("log artifact directory is unsafe")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(artifact_path / "manifest.json", flags)
    except OSError as exc:
        raise LogArtifactLoadError("log artifact manifest is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise LogArtifactLoadError("log artifact manifest is unsafe")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read(_MAX_MANIFEST_BYTES + 1)
        if len(data) > _MAX_MANIFEST_BYTES:
            raise LogArtifactLoadError("log artifact manifest exceeds the verification limit")
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise LogArtifactLoadError("log artifact manifest changed during reconstruction")
        return data
    finally:
        os.close(descriptor)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
