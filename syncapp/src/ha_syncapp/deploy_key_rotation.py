"""Crash-safe staged rotation for one protected Repo B deploy-key generation."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .deploy_key import (
    DeployKeyEnrollment,
    DeployKeyError,
    ensure_repo_b_deploy_key,
    inspect_repo_b_deploy_key,
)
from .deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    test_repo_b_deploy_key_access,
)

_SCHEMA_VERSION = 1
_MAX_RECORD_BYTES = 32_768
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_TARGET = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_PHASES = frozenset({"preparing", "prepared", "verified", "activating", "activated", "blocked"})
_BLOCK_REASONS = frozenset({"generation_failed", "access_failed", "proof_mismatch"})


class DeployKeyRotationError(RuntimeError):
    """Deploy-key rotation state could not be advanced safely."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class DeployKeyRotationStatus:
    """Enrollment-safe, content-free status for one exact rotation request."""

    request_id: str
    rotation_id: str
    phase: str
    target: str
    repository_id: int
    active_generation_id: str
    active_fingerprint: str
    candidate_generation_id: str | None
    candidate_fingerprint: str | None
    candidate_public_key: str | None
    observation_sha256: str | None
    retained_generation_id: str | None
    retained_fingerprint: str | None
    blocked_reason: str | None


def prepare_repo_b_deploy_key_rotation(
    active_directory: Path,
    request_id: str,
    target: str,
    expected_repository_id: int,
    *,
    ssh_keygen: Path = Path("/usr/bin/ssh-keygen"),
) -> DeployKeyRotationStatus:
    """Stage a replacement generation after durably binding one explicit request."""
    _validate_inputs(active_directory, request_id, target, expected_repository_id)
    paths = _paths(active_directory)
    with _rotation_lock(paths):
        if _lexists(paths.record):
            record = _read_record(paths.record)
            _verify_request_binding(record, request_id, target, expected_repository_id)
            return _replay_prepare(paths, record, ssh_keygen)
        active = _inspect_key(active_directory, "active")
        if _lexists(paths.candidate) or _lexists(paths.retained):
            if _lexists(paths.retained):
                raise DeployKeyRotationError("A retained deploy key prevents rotation")
            raise DeployKeyRotationError("Deploy key rotation state is incomplete")
        if _lexists(paths.candidate_journal):
            raise DeployKeyRotationError("Deploy key rotation state is incomplete")

        record = _initial_record(
            request_id=request_id,
            rotation_id=str(uuid4()),
            target=target,
            repository_id=expected_repository_id,
            active=active,
        )
        try:
            _write_record(paths.record, record)
            candidate = ensure_repo_b_deploy_key(paths.candidate, ssh_keygen=ssh_keygen)
            _set_candidate(record, candidate)
            record["phase"] = "prepared"
            _write_record(paths.record, record)
        except (OSError, DeployKeyError, DeployKeyRotationError, RuntimeError):
            record["phase"] = "blocked"
            record["blocked_reason"] = "generation_failed"
            with suppress(OSError):
                _write_record(paths.record, record)
            raise DeployKeyRotationError("Deploy key rotation preparation failed") from None
        return _status(record)


def verify_repo_b_deploy_key_rotation(
    active_directory: Path,
    request_id: str,
    target: str,
    github_token: str,
    expected_repository_id: int,
    *,
    known_hosts_file: Path = Path("/app/github_known_hosts"),
    work_directory: Path,
    git_executable: Path = Path("/usr/bin/git"),
    ssh_executable: Path = Path("/usr/bin/ssh"),
) -> DeployKeyRotationStatus:
    """Prove the exact staged key against the pinned private repository identity."""
    _validate_inputs(active_directory, request_id, target, expected_repository_id)
    paths = _paths(active_directory)
    with _rotation_lock(paths):
        record = _read_record(paths.record)
        _verify_request_binding(record, request_id, target, expected_repository_id)
        _validate_record_generations(paths, record)
        phase = record["phase"]
        if phase == "blocked":
            raise DeployKeyRotationError("Deploy key rotation is blocked")
        if phase == "preparing":
            raise DeployKeyRotationError("Deploy key rotation is incomplete")
        if phase in {"verified", "activating", "activated"}:
            return _status(record)
        if phase != "prepared":
            raise DeployKeyRotationError("Deploy key rotation record is invalid")

        try:
            proof = test_repo_b_deploy_key_access(
                target,
                github_token,
                expected_repository_id,
                paths.candidate,
                known_hosts_file=known_hosts_file,
                work_directory=work_directory,
                git_executable=git_executable,
                ssh_executable=ssh_executable,
            )
        except DeployKeyAccessError as error:
            if error.transient:
                raise DeployKeyRotationError(
                    "Deploy key rotation verification is temporarily unavailable",
                    transient=True,
                ) from None
            record["phase"] = "blocked"
            record["blocked_reason"] = "access_failed"
            _persist_or_fail(paths.record, record)
            raise DeployKeyRotationError("Deploy key rotation is blocked") from None
        if not _proof_matches(record, proof):
            record["phase"] = "blocked"
            record["blocked_reason"] = "proof_mismatch"
            _persist_or_fail(paths.record, record)
            raise DeployKeyRotationError("Deploy key rotation is blocked")
        record["phase"] = "verified"
        record["observation_sha256"] = proof.observation_sha256
        _persist_or_fail(paths.record, record)
        return _status(record)


def activate_repo_b_deploy_key_rotation(
    active_directory: Path,
    request_id: str,
) -> DeployKeyRotationStatus:
    """Activate only a verified replacement while retaining the prior generation."""
    _validate_active_path(active_directory)
    _validate_uuid(request_id, "request")
    paths = _paths(active_directory)
    with _rotation_lock(paths):
        record = _read_record(paths.record)
        if record["request_id"] != request_id:
            raise DeployKeyRotationError("Deploy key rotation does not match this request")
        phase = record["phase"]
        if phase == "blocked":
            raise DeployKeyRotationError("Deploy key rotation is blocked")
        if phase in {"preparing", "prepared"}:
            raise DeployKeyRotationError("Deploy key rotation is not verified")
        if phase == "activated":
            _validate_record_generations(paths, record)
            return _status(record)
        if phase == "verified":
            _validate_record_generations(paths, record)
            record["phase"] = "activating"
            _persist_or_fail(paths.record, record)
        elif phase != "activating":
            raise DeployKeyRotationError("Deploy key rotation record is invalid")
        try:
            _reconcile_activation(paths, record)
            record["phase"] = "activated"
            _write_record(paths.record, record)
        except (OSError, DeployKeyError, DeployKeyRotationError):
            raise DeployKeyRotationError("Deploy key rotation activation was interrupted") from None
        _validate_record_generations(paths, record)
        return _status(record)


def inspect_repo_b_deploy_key_rotation(
    active_directory: Path,
    request_id: str,
) -> DeployKeyRotationStatus:
    """Inspect one exact rotation without generating, testing, or activating a key."""
    _validate_active_path(active_directory)
    _validate_uuid(request_id, "request")
    paths = _paths(active_directory)
    with _rotation_lock(paths):
        record = _read_record(paths.record)
        if record["request_id"] != request_id:
            raise DeployKeyRotationError("Deploy key rotation does not match this request")
        _validate_record_generations(paths, record)
        return _status(record)


@dataclass(frozen=True)
class _RotationPaths:
    active: Path
    record: Path
    lock: Path
    candidate: Path
    candidate_journal: Path
    retained: Path


def _paths(active: Path) -> _RotationPaths:
    parent = active.parent
    stem = f".{active.name}.rotation"
    candidate = parent / f"{stem}-candidate"
    return _RotationPaths(
        active=active,
        record=parent / f"{stem}.json",
        lock=parent / f"{stem}.lock",
        candidate=candidate,
        candidate_journal=parent / f".{candidate.name}.generation.json",
        retained=parent / f"{stem}-retained",
    )


def _validate_inputs(active: Path, request_id: str, target: str, repository_id: int) -> None:
    _validate_active_path(active)
    _validate_uuid(request_id, "request")
    if not isinstance(target, str) or _TARGET.fullmatch(target) is None:
        raise DeployKeyRotationError("Deploy key rotation target is invalid")
    if type(repository_id) is not int or repository_id <= 0:
        raise DeployKeyRotationError("Deploy key rotation repository identity is invalid")


def _validate_active_path(path: Path) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or Path(os.path.normpath(path)) != path
        or _SAFE_NAME.fullmatch(path.name) is None
    ):
        raise DeployKeyRotationError("Deploy key rotation path is invalid")


def _validate_uuid(value: object, label: str) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise DeployKeyRotationError(f"Deploy key rotation {label} identity is invalid") from None


@contextmanager
def _rotation_lock(paths: _RotationPaths) -> Iterator[None]:
    _verify_parent_directory(paths.active.parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(paths.lock, flags, 0o600)
        metadata = os.fstat(descriptor)
        path_metadata = paths.lock.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise DeployKeyRotationError("Deploy key rotation lock is invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployKeyRotationError("Deploy key rotation is busy", transient=True) from None
    except DeployKeyRotationError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise DeployKeyRotationError("Deploy key rotation lock is invalid") from None
    try:
        yield
    finally:
        os.close(descriptor)


def _initial_record(
    *,
    request_id: str,
    rotation_id: str,
    target: str,
    repository_id: int,
    active: DeployKeyEnrollment,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "request_id": request_id,
        "rotation_id": rotation_id,
        "phase": "preparing",
        "target": target,
        "repository_id": repository_id,
        "original_generation_id": active.generation_id,
        "original_fingerprint": active.fingerprint,
        "candidate_generation_id": None,
        "candidate_fingerprint": None,
        "candidate_public_key": None,
        "observation_sha256": None,
        "blocked_reason": None,
    }


def _set_candidate(record: dict[str, object], candidate: DeployKeyEnrollment) -> None:
    record["candidate_generation_id"] = candidate.generation_id
    record["candidate_fingerprint"] = candidate.fingerprint
    record["candidate_public_key"] = candidate.public_key


def _replay_prepare(
    paths: _RotationPaths,
    record: dict[str, Any],
    ssh_keygen: Path,
) -> DeployKeyRotationStatus:
    phase = record["phase"]
    if phase in {"activating", "activated"}:
        _validate_record_generations(paths, record)
        return _status(record)
    active = _inspect_key(paths.active, "active")
    _verify_original(record, active)
    if phase == "blocked":
        if record["blocked_reason"] == "generation_failed":
            raise DeployKeyRotationError("Deploy key rotation state is incomplete")
        raise DeployKeyRotationError("Deploy key rotation is blocked")
    if phase == "preparing":
        try:
            if _lexists(paths.candidate):
                candidate = _inspect_key(paths.candidate, "candidate")
            else:
                candidate = ensure_repo_b_deploy_key(paths.candidate, ssh_keygen=ssh_keygen)
            _set_candidate(record, candidate)
            record["phase"] = "prepared"
            _write_record(paths.record, record)
        except (OSError, DeployKeyError, DeployKeyRotationError, RuntimeError):
            record["phase"] = "blocked"
            record["blocked_reason"] = "generation_failed"
            with suppress(OSError):
                _write_record(paths.record, record)
            raise DeployKeyRotationError("Deploy key rotation state is incomplete") from None
    _validate_record_generations(paths, record)
    return _status(record)


def _verify_request_binding(
    record: dict[str, Any], request_id: str, target: str, repository_id: int
) -> None:
    if record["request_id"] != request_id:
        raise DeployKeyRotationError("Deploy key rotation belongs to another request")
    if record["target"] != target or record["repository_id"] != repository_id:
        raise DeployKeyRotationError("Deploy key rotation does not match repository identity")


def _proof_matches(record: dict[str, Any], proof: DeployKeyAccessProof) -> bool:
    return (
        type(proof) is DeployKeyAccessProof
        and proof.target.casefold() == str(record["target"]).casefold()
        and proof.repository_id == record["repository_id"]
        and proof.key_fingerprint == record["candidate_fingerprint"]
        and proof.generation_id == record["candidate_generation_id"]
        and type(proof.ref_count) is int
        and proof.ref_count >= 0
        and _SHA256.fullmatch(proof.observation_sha256) is not None
    )


def _reconcile_activation(paths: _RotationPaths, record: dict[str, Any]) -> None:
    original_id = record["original_generation_id"]
    candidate_id = record["candidate_generation_id"]
    if not isinstance(candidate_id, str):
        raise DeployKeyRotationError("Deploy key rotation record is invalid")
    for _ in range(3):
        active = _inspect_optional(paths.active)
        candidate = _inspect_optional(paths.candidate)
        retained = _inspect_optional(paths.retained)
        if (
            _generation_id(active) == original_id
            and _generation_id(candidate) == candidate_id
            and retained is None
        ):
            _rename_directory(paths.active, paths.retained)
            continue
        if (
            active is None
            and _generation_id(candidate) == candidate_id
            and _generation_id(retained) == original_id
        ):
            _rename_directory(paths.candidate, paths.active)
            continue
        if (
            _generation_id(active) == candidate_id
            and candidate is None
            and _generation_id(retained) == original_id
        ):
            return
        raise DeployKeyRotationError("Deploy key rotation filesystem state is invalid")
    raise DeployKeyRotationError("Deploy key rotation filesystem state is invalid")


def _rename_directory(source: Path, destination: Path) -> None:
    if _lexists(destination):
        raise DeployKeyRotationError("Deploy key rotation destination already exists")
    os.rename(source, destination)
    _fsync_directory(source.parent)


def _validate_record_generations(paths: _RotationPaths, record: dict[str, Any]) -> None:
    if _lexists(paths.candidate_journal):
        raise DeployKeyRotationError("Deploy key rotation generation changed")
    phase = record["phase"]
    original_id = record["original_generation_id"]
    candidate_id = record["candidate_generation_id"]
    if phase == "activating":
        states = (
            _generation_id(_inspect_optional(paths.active)),
            _generation_id(_inspect_optional(paths.candidate)),
            _generation_id(_inspect_optional(paths.retained)),
        )
        allowed = {
            (original_id, candidate_id, None),
            (None, candidate_id, original_id),
            (candidate_id, None, original_id),
        }
        if states not in allowed:
            raise DeployKeyRotationError("Deploy key rotation generation changed")
        return
    if phase == "activated":
        active = _inspect_key(paths.active, "active")
        retained = _inspect_key(paths.retained, "retained")
        if active.generation_id != candidate_id or retained.generation_id != original_id:
            raise DeployKeyRotationError("Deploy key rotation generation changed")
        if _lexists(paths.candidate):
            raise DeployKeyRotationError("Deploy key rotation generation changed")
        _verify_enrollment_fields(record, active, candidate=True)
        _verify_original(record, retained)
        return

    active = _inspect_key(paths.active, "active")
    _verify_original(record, active)
    if candidate_id is None:
        if phase not in {"preparing", "blocked"} or _lexists(paths.candidate):
            raise DeployKeyRotationError("Deploy key rotation generation changed")
        return
    candidate = _inspect_key(paths.candidate, "candidate")
    _verify_enrollment_fields(record, candidate, candidate=True)
    if _lexists(paths.retained):
        raise DeployKeyRotationError("Deploy key rotation generation changed")


def _verify_original(record: dict[str, Any], enrollment: DeployKeyEnrollment) -> None:
    if (
        enrollment.generation_id != record["original_generation_id"]
        or enrollment.fingerprint != record["original_fingerprint"]
    ):
        raise DeployKeyRotationError("Deploy key rotation generation changed")


def _verify_enrollment_fields(
    record: dict[str, Any], enrollment: DeployKeyEnrollment, *, candidate: bool
) -> None:
    prefix = "candidate" if candidate else "original"
    if (
        enrollment.generation_id != record[f"{prefix}_generation_id"]
        or enrollment.fingerprint != record[f"{prefix}_fingerprint"]
        or (candidate and enrollment.public_key != record["candidate_public_key"])
    ):
        raise DeployKeyRotationError("Deploy key rotation generation changed")


def _inspect_key(path: Path, label: str) -> DeployKeyEnrollment:
    try:
        return inspect_repo_b_deploy_key(path)
    except DeployKeyError:
        raise DeployKeyRotationError(f"Deploy key rotation {label} generation is invalid") from None


def _inspect_optional(path: Path) -> DeployKeyEnrollment | None:
    if not _lexists(path):
        return None
    return _inspect_key(path, "stored")


def _generation_id(enrollment: DeployKeyEnrollment | None) -> str | None:
    return None if enrollment is None else enrollment.generation_id


def _status(record: dict[str, Any]) -> DeployKeyRotationStatus:
    phase = record["phase"]
    activated = phase == "activated"
    active_generation_id = (
        record["candidate_generation_id"] if activated else record["original_generation_id"]
    )
    active_fingerprint = (
        record["candidate_fingerprint"] if activated else record["original_fingerprint"]
    )
    if not isinstance(active_generation_id, str) or not isinstance(active_fingerprint, str):
        raise DeployKeyRotationError("Deploy key rotation record is invalid")
    return DeployKeyRotationStatus(
        request_id=record["request_id"],
        rotation_id=record["rotation_id"],
        phase=phase,
        target=record["target"],
        repository_id=record["repository_id"],
        active_generation_id=active_generation_id,
        active_fingerprint=active_fingerprint,
        candidate_generation_id=record["candidate_generation_id"],
        candidate_fingerprint=record["candidate_fingerprint"],
        candidate_public_key=record["candidate_public_key"],
        observation_sha256=record["observation_sha256"],
        retained_generation_id=record["original_generation_id"] if activated else None,
        retained_fingerprint=record["original_fingerprint"] if activated else None,
        blocked_reason=record["blocked_reason"],
    )


def _write_record(path: Path, record: dict[str, object]) -> None:
    unsigned = dict(record)
    unsigned.pop("record_sha256", None)
    payload = dict(unsigned)
    payload["record_sha256"] = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    raw = _canonical_json(payload)
    if len(raw) > _MAX_RECORD_BYTES:
        raise OSError("rotation record exceeded size limit")
    temporary = path.parent / f".{path.name}.{uuid4()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as destination:
            destination.write(raw)
            destination.flush()
            os.fsync(descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _persist_or_fail(path: Path, record: dict[str, object]) -> None:
    try:
        _write_record(path, record)
    except OSError:
        raise DeployKeyRotationError("Deploy key rotation record persistence failed") from None


def _read_record(path: Path) -> dict[str, Any]:
    expected = {
        "schema_version",
        "request_id",
        "rotation_id",
        "phase",
        "target",
        "repository_id",
        "original_generation_id",
        "original_fingerprint",
        "candidate_generation_id",
        "candidate_fingerprint",
        "candidate_public_key",
        "observation_sha256",
        "blocked_reason",
        "record_sha256",
    }
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or not 0 < before.st_size <= _MAX_RECORD_BYTES
        ):
            raise ValueError
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError
            raw = os.read(descriptor, _MAX_RECORD_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) != before.st_size:
            raise ValueError
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != expected or raw != _canonical_json(value):
            raise ValueError
        _validate_record(value)
        recorded = value["record_sha256"]
        unsigned = dict(value)
        del unsigned["record_sha256"]
        calculated = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
        if not hmac.compare_digest(recorded, calculated):
            raise ValueError
        return value
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        KeyError,
        DeployKeyRotationError,
    ):
        raise DeployKeyRotationError("Deploy key rotation record is invalid") from None


def _validate_record(value: dict[str, Any]) -> None:
    if value["schema_version"] != _SCHEMA_VERSION:
        raise ValueError
    _validate_uuid(value["request_id"], "request")
    _validate_uuid(value["rotation_id"], "rotation")
    if value["phase"] not in _PHASES:
        raise ValueError
    if not isinstance(value["target"], str) or _TARGET.fullmatch(value["target"]) is None:
        raise ValueError
    if type(value["repository_id"]) is not int or value["repository_id"] <= 0:
        raise ValueError
    _validate_uuid(value["original_generation_id"], "generation")
    if (
        not isinstance(value["original_fingerprint"], str)
        or _FINGERPRINT.fullmatch(value["original_fingerprint"]) is None
    ):
        raise ValueError
    candidate_id = value["candidate_generation_id"]
    candidate_fingerprint = value["candidate_fingerprint"]
    candidate_public_key = value["candidate_public_key"]
    candidate_values = (candidate_id, candidate_fingerprint, candidate_public_key)
    if all(item is None for item in candidate_values):
        if value["phase"] not in {"preparing", "blocked"}:
            raise ValueError
    elif any(not isinstance(item, str) for item in candidate_values):
        raise ValueError
    else:
        _validate_uuid(candidate_id, "generation")
        if _FINGERPRINT.fullmatch(candidate_fingerprint) is None:
            raise ValueError
        if not candidate_public_key.startswith("ssh-ed25519 ") or "\n" in candidate_public_key:
            raise ValueError
    observation = value["observation_sha256"]
    if observation is not None and (
        not isinstance(observation, str) or _SHA256.fullmatch(observation) is None
    ):
        raise ValueError
    if value["phase"] in {"verified", "activating", "activated"} and observation is None:
        raise ValueError
    blocked_reason = value["blocked_reason"]
    if value["phase"] == "blocked":
        if blocked_reason not in _BLOCK_REASONS:
            raise ValueError
    elif blocked_reason is not None:
        raise ValueError
    if (
        not isinstance(value["record_sha256"], str)
        or _SHA256.fullmatch(value["record_sha256"]) is None
    ):
        raise ValueError


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
    ).encode("utf-8")


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise DeployKeyRotationError("Deploy key rotation filesystem state is invalid") from None


def _verify_parent_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
        ):
            raise ValueError
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ValueError
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        raise DeployKeyRotationError("Deploy key rotation parent is invalid") from None


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
