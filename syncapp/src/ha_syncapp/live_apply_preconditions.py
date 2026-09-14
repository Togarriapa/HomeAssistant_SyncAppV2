"""Read-only proof of live Home Assistant path preconditions before candidate Apply."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .live_apply_plan import LiveApplyOperation, LiveApplyPlan


class LiveApplyPreconditionError(RuntimeError):
    """The live Home Assistant tree no longer satisfies an exact Apply plan."""


@dataclass(frozen=True, slots=True, init=False)
class LiveApplyPreconditionEvidence:
    """Ephemeral immutable proof issued only by the read-only live-path verifier."""

    deployment_id: str
    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    root: str
    verified_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _OperationSnapshot:
    path: str
    status: str
    baseline_mode: str | None
    baseline_object_id: str | None
    candidate_mode: str | None
    candidate_object_id: str | None
    staged_size: int | None
    staged_sha256: str | None


@dataclass(frozen=True, slots=True)
class _PlanSnapshot:
    deployment_id: str
    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    operations: tuple[_OperationSnapshot, ...]


def prove_live_apply_preconditions(
    plan: LiveApplyPlan,
    homeassistant_root: Path,
) -> LiveApplyPreconditionEvidence:
    """Verify affected live paths against plan baseline metadata without mutating them."""
    if type(plan) is not LiveApplyPlan:
        _reject("Apply plan evidence is invalid")
    root = _validate_root(homeassistant_root)
    before = _snapshot_plan(plan)
    _validate_operations(before.operations)

    verified: list[str] = []
    root_fd = _open_root(root)
    try:
        for operation in before.operations:
            _verify_operation(root_fd, operation)
            verified.append(operation.path)
    except LiveApplyPreconditionError:
        raise
    except (OSError, ValueError):
        _reject("live path inspection failed")
    finally:
        os.close(root_fd)

    if _snapshot_plan(plan) != before:
        _reject("Apply plan changed during inspection")

    evidence = object.__new__(LiveApplyPreconditionEvidence)
    values = {
        "deployment_id": before.deployment_id,
        "target": before.target,
        "repository_id": before.repository_id,
        "baseline_sha": before.baseline_sha,
        "candidate_sha": before.candidate_sha,
        "stage_manifest_sha256": before.stage_manifest_sha256,
        "root": str(root),
        "verified_paths": tuple(verified),
    }
    for name, value in values.items():
        object.__setattr__(evidence, name, value)
    return evidence


def _validate_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        _reject("Home Assistant root is invalid")
    return root


def _open_root(root: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(root, flags)
        info = os.fstat(root_fd)
    except OSError:
        _reject("Home Assistant root is invalid")
    if not stat.S_ISDIR(info.st_mode):
        os.close(root_fd)
        _reject("Home Assistant root is invalid")
    return root_fd


def _snapshot_plan(plan: LiveApplyPlan) -> _PlanSnapshot:
    if type(plan.operations) is not tuple:
        _reject("Apply plan evidence is invalid")
    operations: list[_OperationSnapshot] = []
    for operation in plan.operations:
        if type(operation) is not LiveApplyOperation:
            _reject("Apply plan evidence is invalid")
        operations.append(
            _OperationSnapshot(
                path=operation.path,
                status=operation.status,
                baseline_mode=operation.baseline_mode,
                baseline_object_id=operation.baseline_object_id,
                candidate_mode=operation.candidate_mode,
                candidate_object_id=operation.candidate_object_id,
                staged_size=operation.staged_size,
                staged_sha256=operation.staged_sha256,
            )
        )
    return _PlanSnapshot(
        deployment_id=plan.deployment_id,
        target=plan.target,
        repository_id=plan.repository_id,
        baseline_sha=plan.baseline_sha,
        candidate_sha=plan.candidate_sha,
        stage_manifest_sha256=plan.stage_manifest_sha256,
        operations=tuple(operations),
    )


def _validate_operations(operations: tuple[_OperationSnapshot, ...]) -> None:
    seen: set[str] = set()
    paths = tuple(operation.path for operation in operations)
    if paths != tuple(sorted(paths, key=lambda path: path.encode("utf-8"))):
        _reject("Apply plan operation order is invalid")
    for operation in operations:
        if not _safe_path(operation.path):
            _reject("Apply plan path is unsafe")
        if operation.path in seen:
            _reject("Apply plan contains duplicate affected paths")
        seen.add(operation.path)
        if operation.status == "added":
            if operation.baseline_mode is not None or operation.baseline_object_id is not None:
                _reject("Apply plan baseline precondition is invalid")
        else:
            if operation.status not in {
                "deleted",
                "modified",
                "mode_changed",
                "modified_and_mode_changed",
            }:
                _reject("Apply plan baseline precondition is invalid")
            if operation.baseline_mode not in {"100644", "100755"}:
                _reject("Apply plan baseline precondition is invalid")
            if not _valid_object_id(operation.baseline_object_id):
                _reject("Apply plan baseline precondition is invalid")


def _verify_operation(root_fd: int, operation: _OperationSnapshot) -> None:
    parts = operation.path.split("/")
    parent_fd = _open_parent_chain(root_fd, parts[:-1], operation.status == "added")
    if parent_fd is None:
        return
    try:
        _verify_leaf(parent_fd, parts[-1], operation)
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)


def _open_parent_chain(root_fd: int, parts: list[str], added: bool) -> int | None:
    current_fd = root_fd
    for part in parts:
        try:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_fd,
            )
        except FileNotFoundError:
            if current_fd != root_fd:
                os.close(current_fd)
            if added:
                return None
            _reject("live path precondition mismatch")
        except OSError as error:
            if current_fd != root_fd:
                os.close(current_fd)
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                _reject("live path type is unsafe")
            raise
        if current_fd != root_fd:
            os.close(current_fd)
        current_fd = next_fd
    return current_fd


def _verify_leaf(parent_fd: int, name: str, operation: _OperationSnapshot) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if operation.status == "added":
            return
        _reject("live path precondition mismatch")

    if operation.status == "added":
        _reject("live path precondition mismatch")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _reject("live path type is unsafe")

    baseline_object_id = operation.baseline_object_id
    if baseline_object_id is None:
        _reject("Apply plan baseline precondition is invalid")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            _reject("live path type is unsafe")
        raise
    try:
        opened_info = os.fstat(fd)
        if not stat.S_ISREG(opened_info.st_mode):
            _reject("live path type is unsafe")
        data = _read_all(fd)
    finally:
        os.close(fd)
    if _git_blob_object_id(data, len(baseline_object_id)) != baseline_object_id:
        _reject("live path precondition mismatch")
    if _git_mode(opened_info.st_mode) != operation.baseline_mode:
        _reject("live path precondition mismatch")


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _git_blob_object_id(data: bytes, width: int) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    if width == 40:
        return hashlib.sha1(payload, usedforsecurity=False).hexdigest()
    if width == 64:
        return hashlib.sha256(payload).hexdigest()
    _reject("Apply plan baseline precondition is invalid")


def _git_mode(mode: int) -> str:
    return "100755" if mode & 0o111 else "100644"


def _valid_object_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) not in {40, 64}:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _safe_path(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/"):
        return False
    parts = path.split("/")
    return not any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts)


def _reject(message: str) -> NoReturn:
    raise LiveApplyPreconditionError(message) from None
