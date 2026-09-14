"""Read-only proof of live Home Assistant path preconditions before candidate Apply."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .live_apply_plan import LiveApplyOperation, LiveApplyPlan


class LiveApplyPreconditionError(RuntimeError):
    """The live Home Assistant tree no longer satisfies an exact Apply plan."""


@dataclass(frozen=True, slots=True)
class LiveApplyPreconditionEvidence:
    """Ephemeral immutable proof that affected live paths matched one exact baseline."""

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
    try:
        for operation in before.operations:
            _verify_operation(root, operation)
            verified.append(operation.path)
    except LiveApplyPreconditionError:
        raise
    except (OSError, ValueError):
        _reject("live path inspection failed")

    if _snapshot_plan(plan) != before:
        _reject("Apply plan changed during inspection")

    return LiveApplyPreconditionEvidence(
        deployment_id=before.deployment_id,
        target=before.target,
        repository_id=before.repository_id,
        baseline_sha=before.baseline_sha,
        candidate_sha=before.candidate_sha,
        stage_manifest_sha256=before.stage_manifest_sha256,
        root=str(root),
        verified_paths=tuple(verified),
    )


def _validate_root(root: Path) -> Path:
    if type(root) is not Path or not root.is_absolute():
        _reject("Home Assistant root is invalid")
    try:
        info = root.lstat()
    except OSError:
        _reject("Home Assistant root is invalid")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        _reject("Home Assistant root is invalid")
    return root


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


def _verify_operation(root: Path, operation: _OperationSnapshot) -> None:
    parts = operation.path.split("/")
    _verify_parent_chain(root, parts[:-1])
    target = root.joinpath(*parts)
    try:
        info = target.lstat()
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
    data = target.read_bytes()
    if _git_blob_object_id(data, len(baseline_object_id)) != baseline_object_id:
        _reject("live path precondition mismatch")
    if _git_mode(info.st_mode) != operation.baseline_mode:
        _reject("live path precondition mismatch")


def _verify_parent_chain(root: Path, parts: list[str]) -> None:
    current = root
    for part in parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _reject("live path type is unsafe")


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
