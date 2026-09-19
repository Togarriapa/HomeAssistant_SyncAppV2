"""Read-only reconciliation for one interrupted live Apply filesystem operation."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .apply_authorization import ApplyAuthorization
from .candidate_stage import CandidateStage, CandidateStageEntry, verify_candidate_stage
from .live_apply_intent import LiveApplyIntent, derive_live_apply_intent
from .live_apply_intent_store import load_live_apply_intent
from .live_apply_plan import LiveApplyOperation, LiveApplyPlan
from .live_apply_preconditions import LiveApplyPreconditionEvidence
from .live_apply_progress import transition_live_apply_progress
from .live_apply_progress_store import (
    discover_live_apply_recovery,
    load_live_apply_progress,
    record_live_apply_progress,
)
from .live_apply_reconciliation_store import (
    LiveApplyMutationGuard,
    load_live_apply_mutation_guard,
    load_live_apply_reconciliation,
    record_live_apply_reconciliation,
)
from .stage_prewrite_reproof import StagePrewriteEvidence
from .state import StateError, StateStore


class LiveApplyReconciliationError(RuntimeError):
    """An interrupted live Apply operation could not be reconciled safely."""


@dataclass(frozen=True, slots=True)
class LiveApplyReconciliationResult:
    outcome: str
    operation_index: int
    operation_path_sha256: str
    replayed: bool = False


def reconcile_live_apply_operation(
    store: StateStore,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
    *,
    operation_index: int,
) -> LiveApplyReconciliationResult:
    """Prove one uncertain operation's live outcome without mutation authority."""
    intent = _validate_chain(store, authorization, stage_evidence, stage, plan, preconditions)
    if type(operation_index) is not int or not 0 <= operation_index < len(plan.operations):
        _reject("operation index is invalid")
    operation = plan.operations[operation_index]
    decision = discover_live_apply_recovery(store, plan)
    persisted = load_live_apply_progress(store, intent.deployment_id, operation_index)
    if persisted is None:
        _reject("live Apply reconciliation progress is missing")

    if decision.action == "complete" or (
        decision.action == "blocked" and decision.operation_index == operation_index
    ):
        previous = load_live_apply_reconciliation(store, intent.deployment_id, operation_index)
        if previous is None:
            _reject("live Apply operation is not reconcilable")
        return LiveApplyReconciliationResult(
            previous.outcome,
            operation_index,
            persisted.operation_path_sha256,
            replayed=True,
        )
    if decision.action != "reconcile_uncertain" or decision.operation_index != operation_index:
        _reject("operation is not the uncertain live Apply action")

    try:
        guard = load_live_apply_mutation_guard(store, intent.deployment_id, operation_index)
    except StateError:
        _reject("live Apply mutation guard is invalid")
    if guard is None:
        _reject("live Apply mutation guard is missing")
    if (
        guard.deployment_id,
        guard.operation_index,
        guard.intent_record_sha256,
        guard.operations_sha256,
        guard.operation_path_sha256,
    ) != (
        persisted.deployment_id,
        persisted.operation_index,
        persisted.intent_record_sha256,
        persisted.operations_sha256,
        persisted.operation_path_sha256,
    ):
        _reject("live Apply mutation guard binding mismatch")
    outcome = _observe_outcome(Path(preconditions.root), operation, guard)
    try:
        record_live_apply_reconciliation(
            store,
            persisted.progress,
            plan=plan,
            outcome=outcome,
        )
        destination = "mutation_verified" if outcome == "applied" else "blocked"
        record_live_apply_progress(
            store,
            transition_live_apply_progress(persisted.progress, destination),
            plan=plan,
        )
    except StateError:
        _reject("unable to persist live Apply reconciliation")
    return LiveApplyReconciliationResult(
        outcome,
        operation_index,
        persisted.operation_path_sha256,
    )


def _validate_chain(
    store: StateStore,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
) -> LiveApplyIntent:
    if type(store) is not StateStore:
        _reject("live Apply state store is invalid")
    try:
        intent = derive_live_apply_intent(authorization, stage_evidence, plan, preconditions)
        persisted = load_live_apply_intent(store, intent.deployment_id)
    except Exception:
        _reject("live Apply evidence chain is invalid")
    if persisted is None or (
        persisted.target,
        persisted.repository_id,
        persisted.baseline_sha,
        persisted.candidate_sha,
        persisted.stage_manifest_sha256,
        persisted.homeassistant_root,
        persisted.operations_sha256,
    ) != (
        intent.target,
        intent.repository_id,
        intent.baseline_sha,
        intent.candidate_sha,
        intent.stage_manifest_sha256,
        intent.homeassistant_root,
        intent.operations_sha256,
    ):
        _reject("durable live Apply intent binding mismatch")
    try:
        verify_candidate_stage(stage)
    except Exception:
        _reject("candidate Stage integrity re-verification failed")
    _validate_stage_binding(stage_evidence, stage, plan)
    if (
        type(preconditions) is not LiveApplyPreconditionEvidence
        or preconditions.deployment_id != plan.deployment_id
        or preconditions.target != plan.target
        or preconditions.repository_id != plan.repository_id
        or preconditions.baseline_sha != plan.baseline_sha
        or preconditions.candidate_sha != plan.candidate_sha
        or preconditions.stage_manifest_sha256 != plan.stage_manifest_sha256
        or preconditions.verified_paths != tuple(item.path for item in plan.operations)
    ):
        _reject("live Apply precondition binding mismatch")
    return intent


def _validate_stage_binding(
    evidence: StagePrewriteEvidence, stage: CandidateStage, plan: LiveApplyPlan
) -> None:
    if type(evidence) is not StagePrewriteEvidence or type(stage) is not CandidateStage:
        _reject("candidate Stage evidence is invalid")
    binding = (
        evidence.target,
        evidence.repository_id,
        evidence.candidate_sha,
        evidence.stage_manifest_sha256,
    )
    if binding != (
        stage.target,
        stage.repository_id,
        stage.commit_sha,
        stage.manifest_sha256,
    ) or binding != (
        plan.target,
        plan.repository_id,
        plan.candidate_sha,
        plan.stage_manifest_sha256,
    ):
        _reject("candidate Stage binding mismatch")
    entries = {entry.path: entry for entry in stage.entries}
    if len(entries) != len(stage.entries):
        _reject("candidate Stage entries are invalid")
    for operation in plan.operations:
        entry = entries.get(operation.path)
        if operation.status == "deleted":
            if entry is not None:
                _reject("deleted path unexpectedly exists in candidate Stage")
            continue
        if type(entry) is not CandidateStageEntry or (
            entry.git_mode != operation.candidate_mode
            or entry.object_id != operation.candidate_object_id
            or entry.size != operation.staged_size
            or entry.sha256 != operation.staged_sha256
        ):
            _reject("candidate Stage operation binding mismatch")


def _observe_outcome(
    root: Path, operation: LiveApplyOperation, guard: LiveApplyMutationGuard
) -> str:
    root_fd: int | None = None
    parent_fd: int | None = None
    try:
        root_fd = _open_root(root, guard.root_identity)
        parent_fd = root_fd
        parent_fd = _open_parents(root_fd, operation.path.split("/")[:-1], guard.parent_identities)
        state = _leaf_state(parent_fd, operation.path.split("/")[-1], operation)
    except (LiveApplyReconciliationError, OSError, ValueError):
        return "ambiguous"
    finally:
        if parent_fd is not None and root_fd is not None and parent_fd != root_fd:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)

    candidate = _candidate_state(operation)
    baseline = _baseline_state(operation)
    if state == candidate:
        return "applied"
    if state == baseline:
        return "not_applied"
    return "ambiguous"


def _open_root(root: Path, expected: tuple[int, int]) -> int:
    fd: int | None = None
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
    except OSError:
        if fd is not None:
            os.close(fd)
        _reject("live Apply reconciliation root is unavailable")
    if (info.st_dev, info.st_ino) != expected:
        os.close(fd)
        _reject("live Apply reconciliation root changed")
    return fd


def _open_parents(root_fd: int, parts: list[str], expected: tuple[tuple[int, int], ...]) -> int:
    if len(parts) != len(expected):
        _reject("live Apply reconciliation parent binding is invalid")
    current = root_fd
    for index, part in enumerate(parts):
        next_fd: int | None = None
        try:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            info = os.fstat(next_fd)
        except OSError:
            if next_fd is not None:
                os.close(next_fd)
            if current != root_fd:
                os.close(current)
            _reject("live Apply reconciliation parent changed")
        if (info.st_dev, info.st_ino) != expected[index]:
            os.close(next_fd)
            if current != root_fd:
                os.close(current)
            _reject("live Apply reconciliation parent changed")
        if current != root_fd:
            os.close(current)
        current = next_fd
    return current


def _leaf_state(parent_fd: int, name: str, operation: LiveApplyOperation) -> tuple[str, str] | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _reject("live Apply reconciliation path is unsafe")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            _reject("live Apply reconciliation path is unsafe")
        _reject("live Apply reconciliation path is unavailable")
    try:
        opened = os.fstat(fd)
        data = _read_all(fd)
    finally:
        os.close(fd)
    widths = {
        len(value)
        for value in (operation.baseline_object_id, operation.candidate_object_id)
        if value is not None
    }
    if len(widths) != 1:
        _reject("live Apply reconciliation object identity is invalid")
    return _git_blob_object_id(data, widths.pop()), _git_mode(opened.st_mode)


def _baseline_state(operation: LiveApplyOperation) -> tuple[str | None, str | None] | None:
    if operation.status == "added":
        return None
    return operation.baseline_object_id, operation.baseline_mode


def _candidate_state(operation: LiveApplyOperation) -> tuple[str | None, str | None] | None:
    if operation.status == "deleted":
        return None
    return operation.candidate_object_id, operation.candidate_mode


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
    _reject("live Apply reconciliation object identity is invalid")


def _git_mode(mode: int) -> str:
    return "100755" if mode & 0o111 else "100644"


def _reject(message: str) -> NoReturn:
    raise LiveApplyReconciliationError(message) from None
