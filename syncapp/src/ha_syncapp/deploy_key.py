"""Protected, idempotent Repo B Ed25519 deploy-key generation and inspection."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import stat
import subprocess  # nosec B404 - fixed absolute executable and argument vector only
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

_ALGORITHM = "ssh-ed25519"
_COMMENT = "homeassistant-syncapp-repo-b"
_PRIVATE_NAME = "private_key"
_PUBLIC_NAME = "public_key"
_MANIFEST_NAME = "manifest.json"
_SCHEMA_VERSION = 1
_EXPECTED_ENTRIES = frozenset({_PRIVATE_NAME, _PUBLIC_NAME, _MANIFEST_NAME})

MAX_PRIVATE_KEY_BYTES = 16_384
MAX_PUBLIC_KEY_BYTES = 4_096
MAX_MANIFEST_BYTES = 16_384
MAX_COMMAND_OUTPUT_BYTES = 4_096


class DeployKeyError(RuntimeError):
    """Deploy-key state cannot be trusted or generated safely."""


@dataclass(frozen=True)
class DeployKeyEnrollment:
    """Enrollment-safe public metadata for one protected private key."""

    algorithm: str
    public_key: str
    fingerprint: str
    generation_id: str


def ensure_repo_b_deploy_key(
    key_directory: Path,
    *,
    ssh_keygen: Path = Path("/usr/bin/ssh-keygen"),
) -> DeployKeyEnrollment:
    """Create one protected key generation or replay its verified public metadata."""
    if not key_directory.is_absolute() or Path(os.path.normpath(key_directory)) != key_directory:
        raise DeployKeyError("Deploy key parent is invalid")
    parent = key_directory.parent
    _verify_parent(parent)
    journal = parent / f".{key_directory.name}.generation.json"

    root_metadata = _lstat_optional(key_directory)
    journal_metadata = _lstat_optional(journal)
    if root_metadata is not None:
        enrollment = _inspect_generation(key_directory)
        if journal_metadata is not None:
            journal_id = _read_journal(journal)
            if journal_id != enrollment.generation_id:
                raise DeployKeyError("Deploy key state is incomplete")
            try:
                journal.unlink()
                _fsync_directory(parent)
            except OSError:
                raise DeployKeyError("Deploy key state is incomplete") from None
        return enrollment
    if journal_metadata is not None:
        _read_journal(journal)
        raise DeployKeyError("Deploy key state is incomplete")

    generation_id = str(uuid4())
    temporary = parent / f".{key_directory.name}.{generation_id}.tmp"
    _write_journal(journal, generation_id)
    try:
        temporary.mkdir(mode=0o700)
        temporary.chmod(0o700)
        _verify_directory(temporary, expected_mode=0o700)
        private_key = temporary / _PRIVATE_NAME
        _run_ssh_keygen(
            (
                str(ssh_keygen),
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                _COMMENT,
                "-f",
                str(private_key),
            ),
            cwd=temporary,
        )
        public_key = temporary / f"{_PRIVATE_NAME}.pub"
        public_key.rename(temporary / _PUBLIC_NAME)
        derived = _run_ssh_keygen((str(ssh_keygen), "-y", "-f", str(private_key)), cwd=temporary)
        _prepare_generated_files(temporary, generation_id, derived)
        enrollment = _inspect_generation(temporary)
        os.replace(temporary, key_directory)
        _fsync_directory(parent)
        journal.unlink()
        _fsync_directory(parent)
        return enrollment
    except Exception:
        raise DeployKeyError("Deploy key generation failed") from None


def _prepare_generated_files(root: Path, generation_id: str, derived: bytes) -> None:
    private_path = root / _PRIVATE_NAME
    public_path = root / _PUBLIC_NAME
    _chmod_generated_regular(private_path, mode=0o600)
    _chmod_generated_regular(public_path, mode=0o644)
    private_bytes = _read_regular(private_path, expected_mode=0o600, maximum=MAX_PRIVATE_KEY_BYTES)
    public_bytes = _read_regular(public_path, expected_mode=0o644, maximum=MAX_PUBLIC_KEY_BYTES)
    algorithm, public_key, fingerprint = _parse_public_key(public_bytes)
    derived_algorithm, derived_blob = _parse_derived_public_key(derived)
    public_blob = public_key.split(" ", 2)[1]
    if derived_algorithm != algorithm or derived_blob != public_blob:
        raise DeployKeyError("Deploy key generation failed")
    if not private_bytes.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----\n"):
        raise DeployKeyError("Deploy key generation failed")
    if not private_bytes.endswith(b"-----END OPENSSH PRIVATE KEY-----\n"):
        raise DeployKeyError("Deploy key generation failed")

    payload: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "generation_id": generation_id,
        "algorithm": algorithm,
        "public_key": public_key,
        "fingerprint": fingerprint,
        "private_sha256": hashlib.sha256(private_bytes).hexdigest(),
        "public_sha256": hashlib.sha256(public_bytes).hexdigest(),
    }
    payload["record_sha256"] = _record_digest(payload)
    _write_exclusive(
        root / _MANIFEST_NAME,
        _canonical_json(payload),
        mode=0o600,
    )
    _fsync_directory(root)


def _inspect_generation(root: Path) -> DeployKeyEnrollment:
    try:
        _verify_directory(root, expected_mode=0o700)
        entries = {entry.name for entry in root.iterdir()}
        if entries != _EXPECTED_ENTRIES:
            raise DeployKeyError("Deploy key state is invalid")
        private_bytes = _read_regular(
            root / _PRIVATE_NAME, expected_mode=0o600, maximum=MAX_PRIVATE_KEY_BYTES
        )
        public_bytes = _read_regular(
            root / _PUBLIC_NAME, expected_mode=0o644, maximum=MAX_PUBLIC_KEY_BYTES
        )
        manifest_bytes = _read_regular(
            root / _MANIFEST_NAME, expected_mode=0o600, maximum=MAX_MANIFEST_BYTES
        )
        algorithm, public_key, fingerprint = _parse_public_key(public_bytes)
        manifest = _parse_manifest(manifest_bytes)
        if (
            manifest["algorithm"] != algorithm
            or manifest["public_key"] != public_key
            or manifest["fingerprint"] != fingerprint
            or manifest["private_sha256"] != hashlib.sha256(private_bytes).hexdigest()
            or manifest["public_sha256"] != hashlib.sha256(public_bytes).hexdigest()
        ):
            raise DeployKeyError("Deploy key state is invalid")
        generation_id = manifest["generation_id"]
        return DeployKeyEnrollment(
            algorithm=algorithm,
            public_key=public_key,
            fingerprint=fingerprint,
            generation_id=generation_id,
        )
    except DeployKeyError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise DeployKeyError("Deploy key state is invalid") from None


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    expected = {
        "schema_version",
        "generation_id",
        "algorithm",
        "public_key",
        "fingerprint",
        "private_sha256",
        "public_sha256",
        "record_sha256",
    }
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise DeployKeyError("Deploy key state is invalid") from None
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise DeployKeyError("Deploy key state is invalid")
    if manifest.get("schema_version") != _SCHEMA_VERSION:
        raise DeployKeyError("Deploy key state is invalid")
    string_fields = expected - {"schema_version"}
    if any(not isinstance(manifest.get(field), str) for field in string_fields):
        raise DeployKeyError("Deploy key state is invalid")
    generation_id = manifest["generation_id"]
    try:
        if str(UUID(generation_id)) != generation_id:
            raise ValueError
    except (ValueError, AttributeError):
        raise DeployKeyError("Deploy key state is invalid") from None
    for field in ("private_sha256", "public_sha256", "record_sha256"):
        value = manifest[field]
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise DeployKeyError("Deploy key state is invalid")
    recorded = manifest["record_sha256"]
    unsigned = dict(manifest)
    del unsigned["record_sha256"]
    if not _constant_time_equal(recorded, _record_digest(unsigned)):
        raise DeployKeyError("Deploy key state is invalid")
    if raw != _canonical_json(manifest):
        raise DeployKeyError("Deploy key state is invalid")
    return manifest


def _parse_public_key(raw: bytes) -> tuple[str, str, str]:
    try:
        value = raw.decode("ascii")
    except UnicodeError:
        raise DeployKeyError("Deploy key state is invalid") from None
    if not value.endswith("\n") or value.count("\n") != 1:
        raise DeployKeyError("Deploy key state is invalid")
    line = value[:-1]
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != _ALGORITHM or parts[2] != _COMMENT:
        raise DeployKeyError("Deploy key state is invalid")
    blob = _decode_key_blob(parts[1])
    algorithm_size = int.from_bytes(blob[:4], "big") if len(blob) >= 4 else -1
    start = 4
    end = start + algorithm_size
    if algorithm_size != len(_ALGORITHM) or blob[start:end] != _ALGORITHM.encode("ascii"):
        raise DeployKeyError("Deploy key state is invalid")
    if len(blob) != end + 36 or int.from_bytes(blob[end : end + 4], "big") != 32:
        raise DeployKeyError("Deploy key state is invalid")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode(
        "ascii"
    ).rstrip("=")
    return _ALGORITHM, line, fingerprint


def _parse_derived_public_key(raw: bytes) -> tuple[str, str]:
    if len(raw) > MAX_COMMAND_OUTPUT_BYTES:
        raise DeployKeyError("Deploy key generation failed")
    try:
        value = raw.decode("ascii")
    except UnicodeError:
        raise DeployKeyError("Deploy key generation failed") from None
    if not value.endswith("\n") or value.count("\n") != 1:
        raise DeployKeyError("Deploy key generation failed")
    parts = value[:-1].split(" ")
    if len(parts) not in (2, 3) or parts[0] != _ALGORITHM:
        raise DeployKeyError("Deploy key generation failed")
    if len(parts) == 3 and parts[2] != _COMMENT:
        raise DeployKeyError("Deploy key generation failed")
    _decode_key_blob(parts[1])
    return parts[0], parts[1]


def _decode_key_blob(value: str) -> bytes:
    try:
        blob = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise DeployKeyError("Deploy key state is invalid") from None
    if not blob or base64.b64encode(blob).decode("ascii") != value:
        raise DeployKeyError("Deploy key state is invalid")
    return blob


def _read_journal(path: Path) -> str:
    raw = _read_regular(path, expected_mode=0o600, maximum=MAX_MANIFEST_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise DeployKeyError("Deploy key state is incomplete") from None
    if not isinstance(value, dict) or set(value) != {"schema_version", "generation_id"}:
        raise DeployKeyError("Deploy key state is incomplete")
    generation_id = value.get("generation_id")
    if value.get("schema_version") != _SCHEMA_VERSION or not isinstance(generation_id, str):
        raise DeployKeyError("Deploy key state is incomplete")
    try:
        if str(UUID(generation_id)) != generation_id:
            raise ValueError
    except ValueError:
        raise DeployKeyError("Deploy key state is incomplete") from None
    if raw != _canonical_json(value):
        raise DeployKeyError("Deploy key state is incomplete")
    return generation_id


def _write_journal(path: Path, generation_id: str) -> None:
    try:
        _write_exclusive(
            path,
            _canonical_json({"schema_version": _SCHEMA_VERSION, "generation_id": generation_id}),
            mode=0o600,
        )
        _fsync_directory(path.parent)
    except (OSError, DeployKeyError):
        raise DeployKeyError("Deploy key state is incomplete") from None


def _write_exclusive(path: Path, content: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as destination:
            destination.write(content)
            destination.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _chmod_generated_regular(path: Path, *, mode: int) -> None:
    """Set generated-file permissions without following a substituted link."""
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
        ):
            raise DeployKeyError("Deploy key generation failed")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise DeployKeyError("Deploy key generation failed")
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except DeployKeyError:
        raise
    except OSError:
        raise DeployKeyError("Deploy key generation failed") from None


def _read_regular(path: Path, *, expected_mode: int, maximum: int) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        raise DeployKeyError("Deploy key state is invalid") from None
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != expected_mode
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or not 0 < before.st_size <= maximum
    ):
        raise DeployKeyError("Deploy key state is invalid")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise DeployKeyError("Deploy key state is invalid")
            content = os.read(descriptor, maximum + 1)
            if len(content) != before.st_size or len(content) > maximum:
                raise DeployKeyError("Deploy key state is invalid")
            return content
        finally:
            os.close(descriptor)
    except DeployKeyError:
        raise
    except OSError:
        raise DeployKeyError("Deploy key state is invalid") from None


def _verify_parent(path: Path) -> None:
    try:
        _verify_directory(path, expected_mode=0o700)
    except DeployKeyError:
        raise DeployKeyError("Deploy key parent is invalid") from None


def _verify_directory(path: Path, *, expected_mode: int) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise DeployKeyError("Deploy key state is invalid") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or metadata.st_uid != os.geteuid()
    ):
        raise DeployKeyError("Deploy key state is invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        os.close(descriptor)
    except OSError:
        raise DeployKeyError("Deploy key state is invalid") from None


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise DeployKeyError("Deploy key state is invalid") from None


def _run_ssh_keygen(command: tuple[str, ...], *, cwd: Path) -> bytes:
    if not command or not os.path.isabs(command[0]):
        raise DeployKeyError("Deploy key generation failed")
    try:
        result = subprocess.run(  # nosec B603
            command,
            cwd=cwd,
            env={"PATH": str(Path(command[0]).parent), "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise DeployKeyError("Deploy key generation failed") from None
    if result.returncode != 0 or len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES:
        raise DeployKeyError("Deploy key generation failed")
    return result.stdout


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def _record_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _constant_time_equal(first: str, second: str) -> bool:
    return hmac.compare_digest(first, second)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
