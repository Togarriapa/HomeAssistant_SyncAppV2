"""Crash-safe writer for exactly one verified live Apply filesystem operation."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .apply_authorization import ApplyAuthorization
from .candidate_stage import CandidateStage, CandidateStageEntry, verify_candidate_stage
from .live_apply_intent import LiveApplyIntent, derive_live_apply_intent
from .live_apply_intent_store import PersistedLiveApplyIntent, load_live_apply_intent
from .live_apply_operation_precondition import (
    LiveApplyOperationPreconditionError,
    LiveApplyOperationPreconditionEvidence,
    prove_live_apply_operation_precondition,
)
from .live_apply_plan import LiveApplyOperation, LiveApplyPlan
from .live_apply_preconditions import LiveApplyPreconditionEvidence
from .live_apply_progress import (
    LiveApplyProgress,
    start_live_apply_progress,
    transition_live_apply_progress,
)
from .live_apply_progress_store import (
    LiveApplyRecoveryDecision,
    discover_live_apply_progress,
    discover_live_apply_recovery,
    record_live_apply_progress,
)
from .stage_prewrite_reproof import StagePrewriteEvidence
from .state import StateError, StateStore


class LiveApplyWriterError(RuntimeError):
    """One exact live Apply operation could not be completed safely."""


class _PostconditionMismatch(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LiveApplyWriterResult:
    """Sanitized result for one exact ordered operation."""

    status: str
    operation_index: int
    operation_path_sha256: str
    replayed: bool = False


def apply_live_operation(
    store: StateStore,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
    *,
    operation_index: int,
) -> LiveApplyWriterResult:
    """Apply exactly one operation with durable journal-before-mutation ordering."""
    if type(store) is not StateStore:
        _reject("live Apply state store is invalid")
    try:
        intent = derive_live_apply_intent(authorization, stage_evidence, plan, preconditions)
    except Exception:
        _reject("live Apply evidence chain is invalid")
    persisted = _load_exact_intent(store, intent)
    decision = _recovery_decision(store, plan)
    if decision.action == "complete":
        if type(operation_index) is not int or not 0 <= operation_index < len(plan.operations):
            _reject("operation index is invalid")
        persisted_progress = tuple(
            item
            for item in discover_live_apply_progress(store, plan.deployment_id)
            if item.operation_index == operation_index
        )
        if len(persisted_progress) == 1 and persisted_progress[0].phase == "mutation_verified":
            return LiveApplyWriterResult(
                status="mutation_verified",
                operation_index=operation_index,
                operation_path_sha256=persisted_progress[0].operation_path_sha256,
                replayed=True,
            )
        _reject("live Apply recovery state is inconsistent")
    if decision.action == "reconcile_uncertain":
        _reject("live Apply operation requires explicit reconciliation")
    if decision.action == "blocked":
        _reject("live Apply operation is blocked")
    if decision.action != "start_next" or decision.operation_index != operation_index:
        _reject("operation is not the next safe live Apply action")

    operation = _operation(plan, operation_index)
    try:
        verify_candidate_stage(stage)
    except Exception:
        _reject("candidate Stage integrity re-verification failed")
    _validate_stage_binding(stage_evidence, stage, plan, operation)
    candidate_bytes = _candidate_bytes(stage, operation)

    root = Path(preconditions.root)
    try:
        first_proof = prove_live_apply_operation_precondition(
            plan, root, operation_index=operation_index
        )
    except LiveApplyOperationPreconditionError:
        _reject("live path precondition mismatch before journaling")
    _validate_operation_proof(first_proof, intent, operation, operation_index)

    progress = start_live_apply_progress(
        intent,
        persisted.record_sha256,
        plan,
        operation_index=operation_index,
    )
    try:
        record_live_apply_progress(store, progress, plan=plan)
    except Exception:
        _reject("unable to persist live Apply progress before mutation")

    try:
        second_proof = prove_live_apply_operation_precondition(
            plan, root, operation_index=operation_index
        )
        _validate_operation_proof(second_proof, intent, operation, operation_index)
    except (LiveApplyOperationPreconditionError, LiveApplyWriterError):
        _block_if_possible(store, progress, plan)
        _reject("live path precondition changed after journaling")

    try:
        _mutate_operation(root, operation, candidate_bytes)
    except Exception:
        _reject("live Apply mutation outcome is uncertain")

    try:
        _verify_postcondition(root, operation)
    except _PostconditionMismatch:
        _block_if_possible(store, progress, plan)
        _reject("live Apply postcondition verification failed")
    except Exception:
        _reject("live Apply postcondition outcome is uncertain")

    verified = transition_live_apply_progress(progress, "mutation_verified")
    try:
        durable = record_live_apply_progress(store, verified, plan=plan)
    except Exception:
        _reject("live Apply mutation outcome is uncertain")
    return LiveApplyWriterResult(
        status=durable.phase,
        operation_index=operation_index,
        operation_path_sha256=durable.operation_path_sha256,
    )


def _load_exact_intent(store: StateStore, intent: LiveApplyIntent) -> PersistedLiveApplyIntent:
    try:
        persisted = load_live_apply_intent(store, intent.deployment_id)
    except StateError:
        _reject("durable live Apply intent is invalid")
    if persisted is None:
        _reject("durable live Apply intent is missing")
    observed = (
        persisted.deployment_id,
        persisted.target,
        persisted.repository_id,
        persisted.baseline_sha,
        persisted.candidate_sha,
        persisted.stage_manifest_sha256,
        persisted.backup_slug,
        persisted.homeassistant_root,
        persisted.operations_sha256,
    )
    expected = (
        intent.deployment_id,
        intent.target,
        intent.repository_id,
        intent.baseline_sha,
        intent.candidate_sha,
        intent.stage_manifest_sha256,
        intent.backup_slug,
        intent.homeassistant_root,
        intent.operations_sha256,
    )
    if observed != expected:
        _reject("durable live Apply intent binding mismatch")
    return persisted


def _recovery_decision(store: StateStore, plan: LiveApplyPlan) -> LiveApplyRecoveryDecision:
    try:
        return discover_live_apply_recovery(store, plan)
    except StateError:
        _reject("live Apply recovery state is invalid")


def _operation(plan: LiveApplyPlan, operation_index: int) -> LiveApplyOperation:
    if type(plan) is not LiveApplyPlan:
        _reject("Apply plan evidence is invalid")
    if type(operation_index) is not int or not 0 <= operation_index < len(plan.operations):
        _reject("operation index is invalid")
    operation = plan.operations[operation_index]
    if type(operation) is not LiveApplyOperation:
        _reject("Apply plan operation is invalid")
    return operation


def _validate_stage_binding(
    evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    plan: LiveApplyPlan,
    operation: LiveApplyOperation,
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
    matches = tuple(entry for entry in stage.entries if entry.path == operation.path)
    if operation.status == "deleted":
        if matches:
            _reject("deleted path unexpectedly exists in candidate Stage")
        return
    if len(matches) != 1:
        _reject("candidate Stage operation entry is missing")
    entry = matches[0]
    if type(entry) is not CandidateStageEntry or (
        entry.git_mode != operation.candidate_mode
        or entry.object_id != operation.candidate_object_id
        or entry.size != operation.staged_size
        or entry.sha256 != operation.staged_sha256
    ):
        _reject("candidate Stage operation binding mismatch")


def _candidate_bytes(stage: CandidateStage, operation: LiveApplyOperation) -> bytes | None:
    if operation.status == "deleted":
        return None
    parts = operation.path.split("/")
    tree_fd = _open_directory(stage.tree)
    parent_fd = tree_fd
    try:
        parent_fd = _walk_parent(tree_fd, parts[:-1])
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                _reject("candidate Stage operation source is unsafe")
            data = _read_all(fd)
        finally:
            os.close(fd)
    except LiveApplyWriterError:
        raise
    except OSError:
        _reject("candidate Stage operation source is unavailable")
    finally:
        if parent_fd != tree_fd:
            os.close(parent_fd)
        os.close(tree_fd)
    if (
        operation.staged_size != len(data)
        or operation.staged_sha256 != hashlib.sha256(data).hexdigest()
    ):
        _reject("candidate Stage operation source integrity mismatch")
    candidate_id = operation.candidate_object_id
    if candidate_id is None or _git_blob_object_id(data, len(candidate_id)) != candidate_id:
        _reject("candidate Stage operation source object mismatch")
    return data


def _validate_operation_proof(
    proof: LiveApplyOperationPreconditionEvidence,
    intent: LiveApplyIntent,
    operation: LiveApplyOperation,
    operation_index: int,
) -> None:
    if (
        proof.deployment_id,
        proof.target,
        proof.repository_id,
        proof.baseline_sha,
        proof.candidate_sha,
        proof.stage_manifest_sha256,
        proof.root,
        proof.operation_index,
        proof.path,
        proof.baseline_mode,
        proof.baseline_object_id,
    ) != (
        intent.deployment_id,
        intent.target,
        intent.repository_id,
        intent.baseline_sha,
        intent.candidate_sha,
        intent.stage_manifest_sha256,
        intent.homeassistant_root,
        operation_index,
        operation.path,
        operation.baseline_mode,
        operation.baseline_object_id,
    ):
        _reject("live operation precondition proof binding mismatch")


def _mutate_operation(root: Path, operation: LiveApplyOperation, candidate: bytes | None) -> None:
    parts = operation.path.split("/")
    root_fd = _open_directory(root)
    parent_fd = root_fd
    try:
        parent_fd = _walk_parent(root_fd, parts[:-1])
        name = parts[-1]
        if operation.status in {"added", "modified", "modified_and_mode_changed"}:
            if candidate is None or operation.candidate_mode not in {"100644", "100755"}:
                _reject("candidate mutation data is invalid")
            _replace_bytes(parent_fd, name, candidate, operation.candidate_mode, operation.status)
        elif operation.status == "deleted":
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        elif operation.status == "mode_changed":
            mode = _mode_bits(operation.candidate_mode)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    _reject("live path type is unsafe")
                os.fchmod(fd, mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent_fd)
        else:
            _reject("Apply plan operation status is invalid")
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def _replace_bytes(parent_fd: int, name: str, data: bytes, git_mode: str, status: str) -> None:
    temporary = f".syncapp-apply-{uuid.uuid4().hex}.tmp"
    fd: int | None = None
    created = False
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        created = True
        _write_all(fd, data)
        os.fchmod(fd, _mode_bits(git_mode))
        os.fsync(fd)
        os.close(fd)
        fd = None
        if status == "added":
            os.link(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=parent_fd)
            created = False
        else:
            os.replace(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            created = False
        os.fsync(parent_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if created:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=parent_fd)


def _verify_postcondition(root: Path, operation: LiveApplyOperation) -> None:
    parts = operation.path.split("/")
    root_fd = _open_directory(root)
    parent_fd = root_fd
    try:
        parent_fd = _walk_parent(root_fd, parts[:-1])
        name = parts[-1]
        if operation.status == "deleted":
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise _PostconditionMismatch
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise _PostconditionMismatch from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _PostconditionMismatch
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            data = _read_all(fd)
        finally:
            os.close(fd)
        candidate_id = operation.candidate_object_id
        if (
            candidate_id is None
            or _git_blob_object_id(data, len(candidate_id)) != candidate_id
            or _git_mode(opened.st_mode) != operation.candidate_mode
        ):
            raise _PostconditionMismatch
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def _block_if_possible(store: StateStore, progress: LiveApplyProgress, plan: LiveApplyPlan) -> None:
    try:
        blocked = transition_live_apply_progress(progress, "blocked")
        record_live_apply_progress(store, blocked, plan=plan)
    except Exception:
        pass


def _open_directory(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            _reject("filesystem root is unsafe")
        return fd
    except LiveApplyWriterError:
        raise
    except OSError:
        _reject("filesystem root is unavailable")


def _walk_parent(root_fd: int, parts: list[str]) -> int:
    current = root_fd
    for part in parts:
        try:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
        except OSError as error:
            if current != root_fd:
                os.close(current)
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                _reject("filesystem path type is unsafe")
            _reject("filesystem parent path is unavailable")
        if current != root_fd:
            os.close(current)
        current = next_fd
    return current


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


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
    _reject("Git object identity is invalid")


def _git_mode(mode: int) -> str:
    return "100755" if mode & 0o111 else "100644"


def _mode_bits(git_mode: str | None) -> int:
    if git_mode == "100644":
        return 0o644
    if git_mode == "100755":
        return 0o755
    _reject("candidate mode is invalid")


def _reject(message: str) -> NoReturn:
    raise LiveApplyWriterError(message) from None
