"""Derive non-authoritative durable Apply intent data from the verified live-Apply chain."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import NoReturn

from .apply_authorization import ApplyAuthorization
from .live_apply_plan import LiveApplyOperation, LiveApplyPlan
from .live_apply_preconditions import LiveApplyPreconditionEvidence
from .stage_prewrite_reproof import StagePrewriteEvidence

_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_STATUS = {
    "added",
    "deleted",
    "modified",
    "mode_changed",
    "modified_and_mode_changed",
}
_ALLOWED_MODE = {"100644", "100755"}


class LiveApplyIntentError(RuntimeError):
    """The exact live Apply intent could not be derived safely."""


@dataclass(frozen=True, slots=True, init=False)
class LiveApplyIntent:
    """Immutable recovery metadata; this record is never itself live-write authority."""

    deployment_id: str
    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    backup_slug: str
    homeassistant_root: str
    operations_sha256: str


def derive_live_apply_intent(
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
) -> LiveApplyIntent:
    """Bind the verified chain into deterministic, content-free recovery metadata."""
    if type(authorization) is not ApplyAuthorization:
        _reject("Apply authorization evidence is invalid")
    if type(stage_evidence) is not StagePrewriteEvidence:
        _reject("Stage pre-write evidence is invalid")
    if type(plan) is not LiveApplyPlan:
        _reject("Apply plan evidence is invalid")
    if type(preconditions) is not LiveApplyPreconditionEvidence:
        _reject("live Apply precondition evidence is invalid")

    authorization_binding = (
        authorization.deployment_id,
        authorization.target,
        authorization.repository_id,
        authorization.candidate_sha,
        authorization.stage_manifest_sha256,
    )
    stage_binding = (
        stage_evidence.deployment_id,
        stage_evidence.target,
        stage_evidence.repository_id,
        stage_evidence.candidate_sha,
        stage_evidence.stage_manifest_sha256,
    )
    if stage_binding != authorization_binding:
        _reject("Stage pre-write evidence binding does not match Apply authorization")

    plan_binding = (
        plan.deployment_id,
        plan.target,
        plan.repository_id,
        plan.candidate_sha,
        plan.stage_manifest_sha256,
    )
    if plan_binding != authorization_binding or plan.baseline_sha != authorization.baseline_sha:
        _reject("Apply plan binding does not match Apply authorization")

    precondition_binding = (
        preconditions.deployment_id,
        preconditions.target,
        preconditions.repository_id,
        preconditions.candidate_sha,
        preconditions.stage_manifest_sha256,
    )
    if (
        precondition_binding != authorization_binding
        or preconditions.baseline_sha != authorization.baseline_sha
    ):
        _reject("live Apply precondition binding does not match Apply authorization")

    root = _validate_root(preconditions.root)
    operations = _snapshot_operations(plan.operations)
    paths = tuple(item[0] for item in operations)
    if preconditions.verified_paths != paths:
        _reject("live Apply precondition paths do not match Apply plan")

    operations_sha256 = hashlib.sha256(
        json.dumps(operations, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    intent = object.__new__(LiveApplyIntent)
    values = {
        "deployment_id": authorization.deployment_id,
        "target": authorization.target,
        "repository_id": authorization.repository_id,
        "baseline_sha": authorization.baseline_sha,
        "candidate_sha": authorization.candidate_sha,
        "stage_manifest_sha256": authorization.stage_manifest_sha256,
        "backup_slug": authorization.backup_slug,
        "homeassistant_root": root,
        "operations_sha256": operations_sha256,
    }
    for name, value in values.items():
        object.__setattr__(intent, name, value)
    return intent


def _snapshot_operations(operations: object) -> tuple[tuple[object, ...], ...]:
    if type(operations) is not tuple:
        _reject("Apply plan operations are invalid")
    result: list[tuple[object, ...]] = []
    previous: bytes | None = None
    seen: set[str] = set()
    for operation in operations:
        if type(operation) is not LiveApplyOperation:
            _reject("Apply plan operations are invalid")
        _validate_operation(operation)
        key = operation.path.encode("utf-8")
        if operation.path in seen or (previous is not None and key <= previous):
            _reject("Apply plan operations are not deterministically ordered")
        previous = key
        seen.add(operation.path)
        result.append(
            (
                operation.path,
                operation.status,
                operation.baseline_mode,
                operation.baseline_object_id,
                operation.candidate_mode,
                operation.candidate_object_id,
                operation.staged_size,
                operation.staged_sha256,
            )
        )
    return tuple(result)


def _validate_operation(operation: LiveApplyOperation) -> None:
    if not _safe_path(operation.path) or operation.status not in _ALLOWED_STATUS:
        _reject("Apply plan operation is invalid")
    if operation.baseline_mode is not None and operation.baseline_mode not in _ALLOWED_MODE:
        _reject("Apply plan operation is invalid")
    if operation.candidate_mode is not None and operation.candidate_mode not in _ALLOWED_MODE:
        _reject("Apply plan operation is invalid")
    if operation.baseline_object_id is not None and not _valid_object_id(
        operation.baseline_object_id
    ):
        _reject("Apply plan operation is invalid")
    if operation.candidate_object_id is not None and not _valid_object_id(
        operation.candidate_object_id
    ):
        _reject("Apply plan operation is invalid")
    if operation.staged_size is not None and (
        type(operation.staged_size) is not int or operation.staged_size < 0
    ):
        _reject("Apply plan operation is invalid")
    if operation.staged_sha256 is not None and (
        not isinstance(operation.staged_sha256, str)
        or _SHA256.fullmatch(operation.staged_sha256) is None
    ):
        _reject("Apply plan operation is invalid")


def _validate_root(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        _reject("Home Assistant root binding is invalid")
    root = PurePosixPath(value)
    if str(root) != value or any(part in {".", ".."} for part in root.parts):
        _reject("Home Assistant root binding is invalid")
    return value


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    parts = value.split("/")
    return not any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts)


def _valid_object_id(value: object) -> bool:
    return isinstance(value, str) and _OBJECT_ID.fullmatch(value) is not None


def _reject(message: str) -> NoReturn:
    raise LiveApplyIntentError(message) from None
