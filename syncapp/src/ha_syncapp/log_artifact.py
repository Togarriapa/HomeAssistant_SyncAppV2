"""Deterministic, bounded staging for the README-defined Repo B logs branch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

_CATEGORIES = ("deployments", "home-assistant", "supervisor", "syncapp")
_RETENTION_DAYS = 30
_MAX_RECORDS = 10_000
_MAX_RECORD_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_ARTIFACT_ID = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class LogArtifactError(RuntimeError):
    """A log artifact cannot be created or trusted safely."""


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One explicit log record supplied by a future bounded collector."""

    category: str
    record_id: str
    timestamp: datetime
    message: str


@dataclass(frozen=True, slots=True)
class LogArtifactFile:
    """Integrity evidence for one file in a staged log artifact."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class LogArtifact:
    """Immutable evidence for one current 30-day retained log snapshot."""

    root: Path
    artifact_id: str
    files: tuple[LogArtifactFile, ...]


def build_log_artifact(
    staging_root: Path,
    records: tuple[LogRecord, ...],
    *,
    reference_time: datetime,
) -> LogArtifact:
    """Stage one deterministic current-retention log snapshot outside live Home Assistant."""
    _validate_staging_root(staging_root)
    reference_utc = _normalize_reference_time(reference_time)
    normalized = _normalize_records(records, reference_utc)
    cutoff = reference_utc - timedelta(days=_RETENTION_DAYS)
    retained = tuple(item for item in normalized if item[1] >= cutoff)
    payloads = _render_payloads(retained)

    temporary = Path(tempfile.mkdtemp(prefix=".logs-artifact-", dir=staging_root))
    os.chmod(temporary, 0o700)
    destination_root: Path | None = None
    owns_destination = False
    try:
        data_evidence: list[LogArtifactFile] = []
        total_size = 0
        for relative_path, payload in sorted(payloads.items()):
            total_size += len(payload)
            if total_size > _MAX_ARTIFACT_BYTES:
                raise LogArtifactError("log artifact exceeds encoded byte limit")
            destination = temporary.joinpath(*relative_path.split("/"))
            _ensure_private_parent_directories(temporary, destination.parent)
            _write_private_file(destination, payload)
            data_evidence.append(_file_evidence(relative_path, payload))

        manifest = _manifest_bytes(reference_utc, tuple(data_evidence), retained)
        total_size += len(manifest)
        if total_size > _MAX_ARTIFACT_BYTES:
            raise LogArtifactError("log artifact exceeds encoded byte limit")
        _write_private_file(temporary / "manifest.json", manifest)
        files = tuple(data_evidence) + (_file_evidence("manifest.json", manifest),)
        artifact_id = hashlib.sha256(manifest).hexdigest()
        destination_root = staging_root / artifact_id
        if destination_root.exists() or destination_root.is_symlink():
            raise LogArtifactError("log artifact destination already exists")
        os.replace(temporary, destination_root)
        owns_destination = True
        artifact = LogArtifact(root=destination_root, artifact_id=artifact_id, files=files)
        verify_log_artifact(artifact)
        return artifact
    except Exception as exc:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if owns_destination and destination_root is not None and destination_root.exists():
            shutil.rmtree(destination_root, ignore_errors=True)
        if isinstance(exc, LogArtifactError):
            raise
        raise LogArtifactError("log artifact staging failed closed") from exc


def verify_log_artifact(artifact: LogArtifact) -> None:
    """Reverify all staged log bytes, modes, layout and manifest evidence."""
    if type(artifact) is not LogArtifact:
        raise LogArtifactError("log artifact evidence is invalid")
    if _ARTIFACT_ID.fullmatch(artifact.artifact_id) is None:
        raise LogArtifactError("log artifact identifier is invalid")
    _validate_file_evidence(artifact.files)

    root_before = _safe_lstat(artifact.root)
    root_is_private = (
        stat.S_ISDIR(root_before.st_mode) and stat.S_IMODE(root_before.st_mode) == 0o700
    )
    if not root_is_private:
        raise LogArtifactError("log artifact root is not a private directory")

    expected = {item.path: item for item in artifact.files}
    expected_directories = _expected_directories(expected)
    observed: set[str] = set()
    observed_directories: set[str] = set()
    file_bytes: dict[str, bytes] = {}
    for directory, directory_names, file_names in os.walk(artifact.root, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            node = base / name
            relative = node.relative_to(artifact.root).as_posix()
            node_stat = _safe_lstat(node)
            if (
                relative not in expected_directories
                or not stat.S_ISDIR(node_stat.st_mode)
                or stat.S_IMODE(node_stat.st_mode) != 0o700
            ):
                raise LogArtifactError("log artifact directory layout or mode is invalid")
            observed_directories.add(relative)
        for name in file_names:
            node = base / name
            relative = node.relative_to(artifact.root).as_posix()
            if relative in observed or relative not in expected:
                raise LogArtifactError("log artifact file layout does not match evidence")
            observed.add(relative)
            data = _read_private_regular_file(node)
            file_bytes[relative] = data
            evidence = expected[relative]
            digest_matches = hashlib.sha256(data).hexdigest() == evidence.sha256
            if len(data) != evidence.size or not digest_matches:
                raise LogArtifactError("log artifact bytes do not match evidence")

    if observed != set(expected) or observed_directories != expected_directories:
        raise LogArtifactError("log artifact layout does not match evidence")

    manifest = file_bytes.get("manifest.json")
    if manifest is None or hashlib.sha256(manifest).hexdigest() != artifact.artifact_id:
        raise LogArtifactError("log artifact manifest does not match artifact identifier")
    _verify_manifest(manifest, artifact.files)

    root_after = _safe_lstat(artifact.root)
    if _stat_identity(root_before) != _stat_identity(root_after):
        raise LogArtifactError("log artifact root changed during verification")


def _normalize_reference_time(value: datetime) -> datetime:
    invalid = type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None
    if invalid:
        raise LogArtifactError("log artifact reference time must be timezone-aware")
    return value.astimezone(UTC)


def _normalize_records(
    records: tuple[LogRecord, ...],
    reference_time: datetime,
) -> tuple[tuple[LogRecord, datetime], ...]:
    if type(records) is not tuple or len(records) > _MAX_RECORDS:
        raise LogArtifactError("log artifact record set is invalid or too large")
    normalized: list[tuple[LogRecord, datetime]] = []
    identities: set[tuple[str, str]] = set()
    for record in records:
        if (
            type(record) is not LogRecord
            or record.category not in _CATEGORIES
            or type(record.record_id) is not str
            or _RECORD_ID.fullmatch(record.record_id) is None
            or type(record.message) is not str
            or type(record.timestamp) is not datetime
            or record.timestamp.tzinfo is None
            or record.timestamp.utcoffset() is None
        ):
            raise LogArtifactError("log artifact record metadata is invalid")
        timestamp = record.timestamp.astimezone(UTC)
        if timestamp > reference_time:
            raise LogArtifactError("log artifact record timestamp is in the future")
        identity = (record.category, record.record_id)
        if identity in identities:
            raise LogArtifactError("log artifact contains a duplicate record identity")
        identities.add(identity)
        if len(record.message.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise LogArtifactError("log artifact record exceeds encoded byte limit")
        normalized.append((record, timestamp))
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                item[0].category,
                item[1],
                item[0].record_id,
                item[0].message,
            ),
        )
    )


def _render_payloads(records: tuple[tuple[LogRecord, datetime], ...]) -> dict[str, bytes]:
    grouped: dict[str, list[bytes]] = {category: [] for category in _CATEGORIES}
    for record, timestamp in records:
        payload = {
            "message": record.message,
            "record_id": record.record_id,
            "timestamp": _render_timestamp(timestamp),
        }
        grouped[record.category].append(_canonical_json(payload))
    return {
        f"logs/{category}/records.jsonl": b"".join(grouped[category]) for category in _CATEGORIES
    }


def _manifest_bytes(
    reference_time: datetime,
    files: tuple[LogArtifactFile, ...],
    records: tuple[tuple[LogRecord, datetime], ...],
) -> bytes:
    counts = {category: 0 for category in _CATEGORIES}
    for record, _timestamp in records:
        counts[record.category] += 1
    return _canonical_json(
        {
            "categories": list(_CATEGORIES),
            "files": [
                {"path": item.path, "sha256": item.sha256, "size": item.size} for item in files
            ],
            "record_counts": counts,
            "reference_time": _render_timestamp(reference_time),
            "retention_days": _RETENTION_DAYS,
            "schema": 1,
        }
    )


def _verify_manifest(manifest: bytes, files: tuple[LogArtifactFile, ...]) -> None:
    try:
        payload = json.loads(manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LogArtifactError("log artifact manifest is invalid") from exc
    if type(payload) is not dict:
        raise LogArtifactError("log artifact manifest is invalid")
    data_files = tuple(item for item in files if item.path != "manifest.json")
    expected_file_payload = [
        {"path": item.path, "sha256": item.sha256, "size": item.size} for item in data_files
    ]
    record_counts = payload.get("record_counts")
    valid_record_counts = isinstance(record_counts, dict) and set(record_counts) == set(_CATEGORIES)
    if valid_record_counts:
        assert isinstance(record_counts, dict)
        valid_record_counts = all(
            type(value) is int and value >= 0 for value in record_counts.values()
        )
    if (
        payload.get("schema") != 1
        or payload.get("retention_days") != _RETENTION_DAYS
        or payload.get("categories") != list(_CATEGORIES)
        or payload.get("files") != expected_file_payload
        or not isinstance(payload.get("reference_time"), str)
        or not valid_record_counts
    ):
        raise LogArtifactError("log artifact manifest does not match evidence")
    reference_text = payload["reference_time"]
    assert isinstance(reference_text, str)
    try:
        parsed_reference = datetime.fromisoformat(reference_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LogArtifactError("log artifact manifest reference time is invalid") from exc
    if parsed_reference.tzinfo is None or parsed_reference.utcoffset() != timedelta(0):
        raise LogArtifactError("log artifact manifest reference time is invalid")


def _canonical_json(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise LogArtifactError("log artifact contains invalid JSON data") from exc
    return (rendered + "\n").encode("utf-8")


def _render_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _file_evidence(path: str, data: bytes) -> LogArtifactFile:
    digest = hashlib.sha256(data).hexdigest()
    return LogArtifactFile(path=path, sha256=digest, size=len(data))


def _validate_staging_root(staging_root: Path) -> None:
    if type(staging_root) is not Path or not staging_root.is_absolute():
        raise LogArtifactError("log artifact staging root must be an absolute path")
    root_stat = _safe_lstat(staging_root)
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_IMODE(root_stat.st_mode) != 0o700
        or root_stat.st_nlink < 1
    ):
        raise LogArtifactError("log artifact staging root must be a private directory")


def _ensure_private_parent_directories(root: Path, parent: Path) -> None:
    relative = parent.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            current.mkdir(mode=0o700)
        os.chmod(current, 0o700)


def _write_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LogArtifactError("log artifact file could not be created safely") from exc
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _read_private_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LogArtifactError("log artifact file could not be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise LogArtifactError("log artifact contains an unsafe file entry")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read(_MAX_ARTIFACT_BYTES + 1)
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise LogArtifactError("log artifact file exceeds verification byte limit")
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise LogArtifactError("log artifact file changed during verification")
        return data
    finally:
        os.close(descriptor)


def _validate_file_evidence(files: tuple[LogArtifactFile, ...]) -> None:
    if type(files) is not tuple or not files:
        raise LogArtifactError("log artifact file evidence is invalid")
    observed: set[str] = set()
    for item in files:
        if type(item) is not LogArtifactFile:
            raise LogArtifactError("log artifact file evidence is invalid")
        path = PurePosixPath(item.path)
        if (
            not item.path
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != item.path
            or item.path in observed
            or _SHA256.fullmatch(item.sha256) is None
            or type(item.size) is not int
            or item.size < 0
            or item.size > _MAX_ARTIFACT_BYTES
        ):
            raise LogArtifactError("log artifact file evidence is invalid")
        observed.add(item.path)
    expected_paths = {f"logs/{category}/records.jsonl" for category in _CATEGORIES}
    expected_paths.add("manifest.json")
    if observed != expected_paths:
        raise LogArtifactError("log artifact file evidence layout is invalid")


def _expected_directories(expected: dict[str, LogArtifactFile]) -> set[str]:
    directories: set[str] = set()
    for relative in expected:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise LogArtifactError("log artifact filesystem evidence is unavailable") from exc


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
