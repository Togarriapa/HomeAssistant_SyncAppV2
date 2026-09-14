"""Build immutable path-level Apply plans from freshly re-proven candidate Stage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NoReturn

from .candidate_changes import CandidateChange, CandidateChanges
from .candidate_stage import CandidateStage, CandidateStageEntry, verify_candidate_stage
from .stage_prewrite_reproof import StagePrewriteEvidence

_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_MODES = {"100644", "100755"}
_ALLOWED_STATUSES = {
    "added",
    "deleted",
    "modified",
    "mode_changed",
    "modified_and_mode_changed",
}


class LiveApplyPlanError(RuntimeError):
    """An exact deterministic live Apply plan could not be established safely."""


@dataclass(frozen=True, slots=True)
class LiveApplyOperation:
    """One immutable path-level operation for a later preconditioned writer."""

    path: str
    status: str
    baseline_mode: str | None
    baseline_object_id: str | None
    candidate_mode: str | None
    candidate_object_id: str | None
    staged_size: int | None
    staged_sha256: str | None


@dataclass(frozen=True, slots=True)
class LiveApplyPlan:
    """Ephemeral immutable authorization data for one exact future Apply."""

    deployment_id: str
    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    operations: tuple[LiveApplyOperation, ...]


@dataclass(frozen=True, slots=True)
class _StageEntrySnapshot:
    path: str
    git_mode: str
    object_id: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _ChangeSnapshot:
    path: str
    status: str
    baseline_mode: str | None
    baseline_object_id: str | None
    candidate_mode: str | None
    candidate_object_id: str | None


def build_live_apply_plan(
    evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    changes: CandidateChanges,
) -> LiveApplyPlan:
    """Re-prove Stage integrity and derive a deterministic plan without mutation."""
    _validate_input_types(evidence, stage, changes)
    evidence_before = _evidence_binding(evidence)
    stage_before = _stage_snapshot(stage)
    changes_before = _changes_snapshot(changes)
    _validate_bindings(evidence_before, stage_before, changes_before)

    try:
        verify_candidate_stage(stage)
    except Exception:
        _reject("candidate Stage integrity re-verification failed")

    if _evidence_binding(evidence) != evidence_before:
        _reject("Stage pre-write evidence changed during verification")
    if _stage_snapshot(stage) != stage_before:
        _reject("candidate Stage binding changed during verification")
    if _changes_snapshot(changes) != changes_before:
        _reject("candidate change evidence changed during verification")

    operations = _build_operations(stage_before[5], changes_before[4])
    return LiveApplyPlan(
        deployment_id=evidence_before[0],
        target=evidence_before[1],
        repository_id=evidence_before[2],
        baseline_sha=changes_before[2],
        candidate_sha=evidence_before[3],
        stage_manifest_sha256=evidence_before[4],
        operations=operations,
    )


def _validate_input_types(
    evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    changes: CandidateChanges,
) -> None:
    if type(evidence) is not StagePrewriteEvidence:
        _reject("Stage pre-write evidence is invalid")
    if type(stage) is not CandidateStage:
        _reject("candidate Stage evidence is invalid")
    if type(changes) is not CandidateChanges:
        _reject("candidate change evidence is invalid")


def _evidence_binding(
    evidence: StagePrewriteEvidence,
) -> tuple[str, str, int, str, str]:
    return (
        evidence.deployment_id,
        evidence.target,
        evidence.repository_id,
        evidence.candidate_sha,
        evidence.stage_manifest_sha256,
    )


def _stage_snapshot(
    stage: CandidateStage,
) -> tuple[str, int, str, str, str, tuple[_StageEntrySnapshot, ...]]:
    entries: list[_StageEntrySnapshot] = []
    if type(stage.entries) is not tuple:
        _reject("candidate Stage evidence is invalid")
    for entry in stage.entries:
        if type(entry) is not CandidateStageEntry:
            _reject("candidate Stage evidence is invalid")
        entries.append(
            _StageEntrySnapshot(
                path=entry.path,
                git_mode=entry.git_mode,
                object_id=entry.object_id,
                size=entry.size,
                sha256=entry.sha256,
            )
        )
    return (
        stage.target,
        stage.repository_id,
        stage.branch,
        stage.commit_sha,
        stage.manifest_sha256,
        tuple(entries),
    )


def _changes_snapshot(
    changes: CandidateChanges,
) -> tuple[str, int, str, str, tuple[_ChangeSnapshot, ...]]:
    if type(changes.changes) is not tuple:
        _reject("candidate change evidence is invalid")
    items: list[_ChangeSnapshot] = []
    for change in changes.changes:
        if type(change) is not CandidateChange:
            _reject("candidate change evidence is invalid")
        items.append(
            _ChangeSnapshot(
                path=change.path,
                status=change.status,
                baseline_mode=change.baseline_mode,
                baseline_object_id=change.baseline_object_id,
                candidate_mode=change.candidate_mode,
                candidate_object_id=change.candidate_object_id,
            )
        )
    return (
        changes.target,
        changes.repository_id,
        changes.baseline_sha,
        changes.candidate_sha,
        tuple(items),
    )


def _validate_bindings(
    evidence: tuple[str, str, int, str, str],
    stage: tuple[str, int, str, str, str, tuple[_StageEntrySnapshot, ...]],
    changes: tuple[str, int, str, str, tuple[_ChangeSnapshot, ...]],
) -> None:
    deployment_id, target, repository_id, candidate_sha, manifest_sha256 = evidence
    if (
        not isinstance(deployment_id, str)
        or not deployment_id
        or not isinstance(target, str)
        or not target
        or type(repository_id) is not int
        or repository_id <= 0
        or not _valid_object_id(candidate_sha)
        or not isinstance(manifest_sha256, str)
        or _SHA256.fullmatch(manifest_sha256) is None
    ):
        _reject("Stage pre-write evidence binding is invalid")

    if stage[:5] != (
        target,
        repository_id,
        "candidate",
        candidate_sha,
        manifest_sha256,
    ):
        _reject("candidate Stage binding does not match Stage pre-write evidence")
    if (
        changes[0] != target
        or changes[1] != repository_id
        or changes[3] != candidate_sha
    ):
        _reject("candidate change binding does not match Stage pre-write evidence")
    if not _valid_object_id(changes[2]):
        _reject("candidate change baseline binding is invalid")


def _build_operations(
    stage_entries: tuple[_StageEntrySnapshot, ...],
    changes: tuple[_ChangeSnapshot, ...],
) -> tuple[LiveApplyOperation, ...]:
    staged_by_path: dict[str, _StageEntrySnapshot] = {}
    for entry in stage_entries:
        _validate_stage_entry(entry)
        if entry.path in staged_by_path:
            _reject("candidate Stage contains duplicate path evidence")
        staged_by_path[entry.path] = entry

    seen: set[str] = set()
    operations: list[LiveApplyOperation] = []
    for change in changes:
        _validate_change(change)
        if change.path in seen:
            _reject("candidate change evidence contains duplicate paths")
        seen.add(change.path)
        staged = staged_by_path.get(change.path)
        if change.status == "deleted":
            if staged is not None:
                _reject("deleted path still exists in candidate staged tree")
            staged_size = None
            staged_sha256 = None
        else:
            if staged is None or (
                staged.git_mode != change.candidate_mode
                or staged.object_id != change.candidate_object_id
            ):
                _reject("candidate change does not match exact staged entry")
            staged_size = staged.size
            staged_sha256 = staged.sha256
        operations.append(
            LiveApplyOperation(
                path=change.path,
                status=change.status,
                baseline_mode=change.baseline_mode,
                baseline_object_id=change.baseline_object_id,
                candidate_mode=change.candidate_mode,
                candidate_object_id=change.candidate_object_id,
                staged_size=staged_size,
                staged_sha256=staged_sha256,
            )
        )
    operations.sort(key=lambda operation: operation.path.encode("utf-8"))
    return tuple(operations)


def _validate_stage_entry(entry: _StageEntrySnapshot) -> None:
    if (
        not _safe_path(entry.path)
        or entry.git_mode not in _ALLOWED_MODES
        or not _valid_object_id(entry.object_id)
        or type(entry.size) is not int
        or entry.size < 0
        or not isinstance(entry.sha256, str)
        or _SHA256.fullmatch(entry.sha256) is None
    ):
        _reject("candidate Stage entry evidence is invalid")


def _validate_change(change: _ChangeSnapshot) -> None:
    if not _safe_path(change.path) or change.status not in _ALLOWED_STATUSES:
        _reject("candidate change evidence is invalid")
    baseline_present = _valid_git_side(change.baseline_mode, change.baseline_object_id)
    candidate_present = _valid_git_side(change.candidate_mode, change.candidate_object_id)

    if change.status == "added":
        valid = (
            change.baseline_mode is None
            and change.baseline_object_id is None
            and candidate_present
        )
    elif change.status == "deleted":
        valid = (
            baseline_present
            and change.candidate_mode is None
            and change.candidate_object_id is None
        )
    elif change.status == "modified":
        valid = (
            baseline_present
            and candidate_present
            and change.baseline_mode == change.candidate_mode
            and change.baseline_object_id != change.candidate_object_id
        )
    elif change.status == "mode_changed":
        valid = (
            baseline_present
            and candidate_present
            and change.baseline_mode != change.candidate_mode
            and change.baseline_object_id == change.candidate_object_id
        )
    else:
        valid = (
            baseline_present
            and candidate_present
            and change.baseline_mode != change.candidate_mode
            and change.baseline_object_id != change.candidate_object_id
        )
    if not valid:
        _reject("candidate change evidence is internally inconsistent")


def _valid_git_side(mode: str | None, object_id: str | None) -> bool:
    return mode in _ALLOWED_MODES and _valid_object_id(object_id)


def _valid_object_id(value: object) -> bool:
    return isinstance(value, str) and _OBJECT_ID.fullmatch(value) is not None


def _safe_path(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/"):
        return False
    parts = path.split("/")
    return not any(
        part in {"", ".", ".."} or part.casefold() == ".git" for part in parts
    )


def _reject(message: str) -> NoReturn:
    raise LiveApplyPlanError(message) from None
