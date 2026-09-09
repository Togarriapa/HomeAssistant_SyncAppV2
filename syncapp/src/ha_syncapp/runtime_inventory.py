"""Deterministic, integrity-bound staging for the README-defined runtime branch."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_HOMEASSISTANT_DEFAULTS: dict[str, object] = {
    "entities": [],
    "devices": [],
    "integrations": [],
    "areas": [],
    "floors": [],
    "labels": [],
    "services": [],
    "states": [],
}
_SUPERVISOR_DEFAULTS: dict[str, object] = {
    "apps": [],
    "repositories": [],
    "backups": [],
    "system": {},
}
_HARDWARE_DEFAULTS: dict[str, object] = {
    "system": {},
    "storage": {},
    "network": {},
}
_ANALYSIS_DEFAULTS: dict[str, object] = {
    "topology": {},
    "dependencies": {},
    "unavailable_entities": [],
    "unknown_entities": [],
    "orphan_entities": [],
    "orphan_devices": [],
    "integration_health": {},
}
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_ID = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_DEPTH = 64


class RuntimeInventoryError(RuntimeError):
    """Runtime inventory evidence cannot be created or trusted safely."""


@dataclass(frozen=True, slots=True)
class RuntimeInventoryInput:
    """Explicit already-collected datasets for one runtime inventory artifact."""

    manifest: Mapping[str, object]
    homeassistant: Mapping[str, object] = field(default_factory=dict)
    supervisor: Mapping[str, object] = field(default_factory=dict)
    hardware: Mapping[str, object] = field(default_factory=dict)
    analysis: Mapping[str, object] = field(default_factory=dict)
    deployments: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeInventoryFile:
    """Integrity evidence for one staged runtime JSON file."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class RuntimeInventoryArtifact:
    """Immutable evidence for a complete staged runtime inventory."""

    root: Path
    artifact_id: str
    files: tuple[RuntimeInventoryFile, ...]


def build_runtime_inventory(
    staging_root: Path,
    inventory: RuntimeInventoryInput,
) -> RuntimeInventoryArtifact:
    """Build and verify one isolated deterministic runtime inventory artifact."""
    _validate_staging_root(staging_root)
    if type(inventory) is not RuntimeInventoryInput:
        raise RuntimeInventoryError("runtime inventory input is invalid")

    payloads = _inventory_payloads(inventory)
    temporary = Path(tempfile.mkdtemp(prefix=".runtime-inventory-", dir=staging_root))
    destination_root: Path | None = None
    owns_destination = False
    try:
        evidence: list[RuntimeInventoryFile] = []
        for relative_path, payload in sorted(payloads.items()):
            encoded = _canonical_json(payload)
            destination = temporary / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write_file(destination, encoded)
            evidence.append(
                RuntimeInventoryFile(
                    path=relative_path,
                    sha256=hashlib.sha256(encoded).hexdigest(),
                    size=len(encoded),
                )
            )
        files = tuple(evidence)
        artifact_id = _artifact_id(files)
        destination_root = staging_root / artifact_id
        if destination_root.exists() or destination_root.is_symlink():
            raise RuntimeInventoryError("runtime inventory artifact destination already exists")
        os.replace(temporary, destination_root)
        owns_destination = True
        artifact = RuntimeInventoryArtifact(destination_root, artifact_id, files)
        verify_runtime_inventory(artifact)
        return artifact
    except Exception as exc:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if owns_destination and destination_root is not None and destination_root.exists():
            shutil.rmtree(destination_root, ignore_errors=True)
        if isinstance(exc, RuntimeInventoryError):
            raise
        raise RuntimeInventoryError("runtime inventory staging failed closed") from exc


def verify_runtime_inventory(artifact: RuntimeInventoryArtifact) -> None:
    """Reverify staged bytes and layout against immutable runtime evidence."""
    if type(artifact) is not RuntimeInventoryArtifact:
        raise RuntimeInventoryError("runtime inventory artifact evidence is invalid")
    if not _ARTIFACT_ID.fullmatch(artifact.artifact_id):
        raise RuntimeInventoryError("runtime inventory artifact identifier is invalid")
    _validate_file_evidence(artifact.files)
    if _artifact_id(artifact.files) != artifact.artifact_id:
        raise RuntimeInventoryError("runtime inventory artifact evidence is inconsistent")

    root_before = _safe_lstat(artifact.root)
    if not stat.S_ISDIR(root_before.st_mode):
        raise RuntimeInventoryError("runtime inventory root is not a safe directory")

    expected = {entry.path: entry for entry in artifact.files}
    expected_directories = _expected_directories(expected)
    observed: set[str] = set()
    observed_directories: set[str] = set()
    for directory, directory_names, file_names in os.walk(artifact.root, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            node = base / name
            relative = node.relative_to(artifact.root).as_posix()
            if relative not in expected_directories:
                raise RuntimeInventoryError("runtime inventory directory layout does not match evidence")
            node_stat = _safe_lstat(node)
            if not stat.S_ISDIR(node_stat.st_mode):
                raise RuntimeInventoryError("runtime inventory contains an unsafe directory entry")
            observed_directories.add(relative)
        for name in file_names:
            node = base / name
            relative = node.relative_to(artifact.root).as_posix()
            if relative in observed or relative not in expected:
                raise RuntimeInventoryError("runtime inventory file layout does not match evidence")
            observed.add(relative)
            data = _read_stable_regular_file(node)
            entry = expected[relative]
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise RuntimeInventoryError("runtime inventory staged bytes do not match evidence")
    if observed != set(expected) or observed_directories != expected_directories:
        raise RuntimeInventoryError("runtime inventory file layout does not match evidence")

    root_after = _safe_lstat(artifact.root)
    if _stat_identity(root_before) != _stat_identity(root_after):
        raise RuntimeInventoryError("runtime inventory root changed during verification")


def _inventory_payloads(inventory: RuntimeInventoryInput) -> dict[str, object]:
    payloads: dict[str, object] = {"manifest.json": dict(inventory.manifest)}
    _add_section(
        payloads,
        "homeassistant",
        inventory.homeassistant,
        _HOMEASSISTANT_DEFAULTS,
    )
    _add_section(payloads, "supervisor", inventory.supervisor, _SUPERVISOR_DEFAULTS)
    _add_section(payloads, "hardware", inventory.hardware, _HARDWARE_DEFAULTS)
    _add_section(payloads, "analysis", inventory.analysis, _ANALYSIS_DEFAULTS)

    for commit_sha, payload in inventory.deployments.items():
        if not isinstance(commit_sha, str) or not _COMMIT_SHA.fullmatch(commit_sha):
            raise RuntimeInventoryError("runtime deployment record commit is invalid")
        payloads[f"deployments/{commit_sha}.json"] = payload
    return payloads


def _add_section(
    payloads: dict[str, object],
    directory: str,
    supplied: Mapping[str, object],
    defaults: Mapping[str, object],
) -> None:
    if any(not isinstance(key, str) for key in supplied):
        raise RuntimeInventoryError("runtime inventory section contains unsupported dataset keys")
    unknown = set(supplied) - set(defaults)
    if unknown:
        raise RuntimeInventoryError("runtime inventory section contains unsupported dataset keys")
    for name, default in defaults.items():
        payloads[f"{directory}/{name}.json"] = supplied.get(name, default)


def _canonical_json(value: object) -> bytes:
    _validate_json_value(value, depth=0)
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeInventoryError("runtime inventory contains invalid JSON data") from exc
    return (rendered + "\n").encode("utf-8")


def _validate_json_value(value: object, *, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise RuntimeInventoryError("runtime inventory JSON nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeInventoryError("runtime inventory contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeInventoryError("runtime inventory object keys must be strings")
            _validate_json_value(item, depth=depth + 1)
        return
    raise RuntimeInventoryError("runtime inventory contains a non-JSON value")


def _validate_file_evidence(files: tuple[RuntimeInventoryFile, ...]) -> None:
    observed: set[str] = set()
    for entry in files:
        if type(entry) is not RuntimeInventoryFile:
            raise RuntimeInventoryError("runtime inventory file evidence is invalid")
        path = PurePosixPath(entry.path)
        if (
            not entry.path
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix != ".json"
            or path.as_posix() != entry.path
        ):
            raise RuntimeInventoryError("runtime inventory file evidence path is invalid")
        if entry.path in observed:
            raise RuntimeInventoryError("runtime inventory evidence contains duplicate paths")
        observed.add(entry.path)
        if not _SHA256.fullmatch(entry.sha256) or type(entry.size) is not int or entry.size < 0:
            raise RuntimeInventoryError("runtime inventory file evidence metadata is invalid")


def _expected_directories(expected: Mapping[str, RuntimeInventoryFile]) -> set[str]:
    directories: set[str] = set()
    for relative in expected:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _artifact_id(files: tuple[RuntimeInventoryFile, ...]) -> str:
    digest = hashlib.sha256()
    for entry in files:
        digest.update(entry.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _read_stable_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeInventoryError("runtime inventory file could not be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeInventoryError("runtime inventory contains an unsafe file entry")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read()
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise RuntimeInventoryError("runtime inventory file changed during verification")
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


def _write_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as destination:
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
    finally:
        os.close(descriptor)


def _validate_staging_root(staging_root: Path) -> None:
    if not isinstance(staging_root, Path) or not staging_root.is_absolute():
        raise RuntimeInventoryError("runtime inventory staging root is invalid")
    node_stat = _safe_lstat(staging_root)
    if not stat.S_ISDIR(node_stat.st_mode):
        raise RuntimeInventoryError("runtime inventory staging root is not a safe directory")


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise RuntimeInventoryError("runtime inventory filesystem evidence is unavailable") from exc
