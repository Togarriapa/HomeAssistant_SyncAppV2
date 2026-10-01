"""Crash-reconcilable execution of one explicit Repo B initialization authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from .deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
    read_repo_b_deploy_key_references,
)
from .git_workspace import GitWorkspace, WorkspaceError, prepare_git_workspace
from .local_git import GitError, create_snapshot_commit, initialize_repository
from .main_routing import build_main_path_router
from .publication_intent import PublicationIntent
from .publication_transport import (
    PublicationTransportError,
    push_publication_intent_with_deploy_key,
)
from .repo_initialization import (
    RepoBInitializationAuthority,
    RepoBInitializationError,
    _authority_from_row,
    load_repo_b_initialization,
)
from .repo_initialization import (
    _record_digest as _authority_digest,
)
from .snapshot import Snapshot, SnapshotError, capture_snapshot
from .state import StateError, StateStore, _parse_timestamp, _timestamp

MAX_INITIALIZATION_ATTEMPTS = 8
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_TARGET = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9._-]{1,100}"
)
_PHASES = {"prepared", "publishing", "retry", "completed", "blocked"}
_BLOCK_REASONS = {
    "none",
    "authority_invalid",
    "key_changed",
    "source_changed",
    "repository_diverged",
    "transport_rejected",
    "attempts_exhausted",
}


class RepoBInitializationExecutionError(RuntimeError):
    """An authorized Repo B initialization failed closed."""


@dataclass(frozen=True, slots=True)
class RepoBInitializationExecution:
    """Integrity-protected durable state for one exact first publication."""

    request_id: str
    target: str
    repository_id: int
    key_fingerprint: str
    generation_id: str
    snapshot_id: str
    commit_sha: str
    phase: str
    block_reason: str
    attempt_count: int
    prepared_at: datetime
    updated_at: datetime
    next_attempt_at: datetime | None
    terminal_at: datetime | None
    record_sha256: str


def execute_authorized_repo_b_initialization(
    store: StateStore,
    request_id: str,
    source: Path,
    snapshot_root: Path,
    workspace_root: Path,
    proof: DeployKeyAccessProof,
    key_directory: Path,
    *,
    known_hosts_file: Path = Path("/app/github_known_hosts"),
    git_executable: Path = Path("/usr/bin/git"),
    ssh_executable: Path = Path("/usr/bin/ssh"),
    now: datetime | None = None,
    recorder_database: Path | None = None,
) -> RepoBInitializationExecution:
    """Execute or reconcile exactly one journaled empty-repository authority."""

    if type(store) is not StateStore or type(request_id) is not str:
        _invalid()
    when = _canonical_time(now)
    snapshot: Snapshot | None = None
    workspace: GitWorkspace | None = None
    probe_directory: Path | None = None
    try:
        existing = load_repo_b_initialization_execution(store, request_id)
        if existing is not None and (existing.prepared_at > when or existing.updated_at > when):
            _invalid()
        authority = load_repo_b_initialization(store, request_id)
        if authority is None or authority.recorded_at > when:
            _invalid()
        if existing is not None and existing.phase in {"completed", "blocked"}:
            _validate_terminal_replay(store, existing, authority)
            return existing
        if (
            existing is not None
            and existing.phase == "retry"
            and existing.next_attempt_at is not None
            and when < existing.next_attempt_at
        ):
            _validate_execution_authority_binding(existing, authority)
            return existing
        if authority.phase != "authorized":
            if existing is None:
                _invalid()
            return _block(store, existing, authority, "authority_invalid", when)

        remote_proven_empty = False
        if existing is not None:
            _validate_execution_authority_binding(existing, authority)
            if not _proof_matches_authority(proof, authority):
                return _block(store, existing, authority, "key_changed", when)
            if store.synchronization_baseline(existing.target, "main") is not None:
                return _block(store, existing, authority, "authority_invalid", when)
            probe_directory = _create_probe_directory(workspace_root)
            recovered_references = _read_references(
                existing,
                proof,
                key_directory,
                probe_directory,
                known_hosts_file,
                git_executable,
                ssh_executable,
            )
            if _is_exact_completed_remote(recovered_references, existing):
                return _complete(store, existing, authority, when)
            if recovered_references.references:
                return _block(store, existing, authority, "repository_diverged", when)
            remote_proven_empty = True

        prepared_at = existing.prepared_at if existing is not None else when
        snapshot = capture_snapshot(
            source,
            snapshot_root,
            include_path=build_main_path_router(source, recorder_database),
        )
        workspace = prepare_git_workspace(snapshot.root, workspace_root)
        initialize_repository(workspace, default_branch="main")
        commit_sha = create_snapshot_commit(workspace, committed_at=prepared_at)
        if commit_sha is None:
            _invalid()

        if existing is None:
            existing = _prepare(store, authority, snapshot.snapshot_id, commit_sha, prepared_at)
        elif existing.snapshot_id != snapshot.snapshot_id or existing.commit_sha != commit_sha:
            return _block(store, existing, authority, "source_changed", when)

        if store.synchronization_baseline(existing.target, "main") is not None:
            return _block(store, existing, authority, "authority_invalid", when)

        if not _proof_matches_authority(proof, authority):
            return _block(store, existing, authority, "key_changed", when)

        if not remote_proven_empty:
            references = _read_references(
                existing,
                proof,
                key_directory,
                workspace.tree_path,
                known_hosts_file,
                git_executable,
                ssh_executable,
            )
            if _is_exact_completed_remote(references, existing):
                return _complete(store, existing, authority, when)
            if references.references:
                return _block(store, existing, authority, "repository_diverged", when)
        if existing.attempt_count >= MAX_INITIALIZATION_ATTEMPTS:
            return _block(store, existing, authority, "attempts_exhausted", when)

        publishing = _transition(
            store,
            existing,
            phase="publishing",
            block_reason="none",
            attempt_count=existing.attempt_count + 1,
            updated_at=when,
            next_attempt_at=None,
            terminal_at=None,
        )
        intent = PublicationIntent(
            publishing.target,
            publishing.repository_id,
            "main",
            publishing.commit_sha,
            None,
            True,
        )
        try:
            pushed = push_publication_intent_with_deploy_key(
                workspace,
                intent,
                proof,
                key_directory,
                known_hosts_file=known_hosts_file,
                git_executable=git_executable,
                ssh_executable=ssh_executable,
                require_repository_empty=True,
            )
        except PublicationTransportError as error:
            return _transport_failure(store, publishing, authority, error, when)
        if pushed != publishing.commit_sha:
            return _block(store, publishing, authority, "transport_rejected", when)

        try:
            confirmed = _read_references(
                publishing,
                proof,
                key_directory,
                workspace.tree_path,
                known_hosts_file,
                git_executable,
                ssh_executable,
            )
        except DeployKeyAccessError as error:
            return _access_failure(store, publishing, authority, error, when)
        if not _is_exact_completed_remote(confirmed, publishing):
            return _block(store, publishing, authority, "repository_diverged", when)
        return _complete(store, publishing, authority, when)
    except (RepoBInitializationExecutionError, KeyboardInterrupt, SystemExit):
        raise
    except DeployKeyAccessError as error:
        if existing is not None and authority is not None:
            return _access_failure(store, existing, authority, error, when)
        _invalid()
    except (
        RepoBInitializationError,
        StateError,
        SnapshotError,
        WorkspaceError,
        GitError,
        sqlite3.Error,
        TypeError,
        ValueError,
        AttributeError,
        OSError,
    ):
        _invalid()
    finally:
        if workspace is not None:
            shutil.rmtree(workspace.root, ignore_errors=True)
        if snapshot is not None:
            shutil.rmtree(snapshot.root, ignore_errors=True)
        if probe_directory is not None:
            shutil.rmtree(probe_directory, ignore_errors=True)


def load_repo_b_initialization_execution(
    store: StateStore, request_id: str
) -> RepoBInitializationExecution | None:
    """Load and integrity-check one execution record."""

    if type(store) is not StateStore or type(request_id) is not str:
        _invalid()
    try:
        if str(UUID(request_id)) != request_id:
            _invalid()
        rows = store._connection.execute(
            "SELECT request_id,target,repository_id,key_fingerprint,generation_id,"
            "snapshot_id,commit_sha,phase,block_reason,attempt_count,prepared_at,"
            "updated_at,next_attempt_at,terminal_at,record_sha256 "
            "FROM repo_b_initialization_execution WHERE request_id=?",
            (request_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid()
        return _execution_from_row(rows[0])
    except RepoBInitializationExecutionError:
        raise
    except (sqlite3.Error, StateError, ValueError, TypeError, AttributeError):
        _invalid()


def next_repo_b_initialization_attempt_at(
    execution: RepoBInitializationExecution,
) -> datetime:
    """Return the bounded exponential retry time for a retry record."""

    if (
        type(execution) is not RepoBInitializationExecution
        or execution.phase != "retry"
        or not 1 <= execution.attempt_count < MAX_INITIALIZATION_ATTEMPTS
        or _execution_from_row(_execution_row(execution)) != execution
    ):
        _invalid()
    return _retry_time(execution.updated_at, execution.attempt_count)


def _retry_time(updated_at: datetime, attempt_count: int) -> datetime:
    delay_minutes = min(60, 2 ** min(attempt_count, 5))
    return updated_at + timedelta(minutes=delay_minutes)


def _read_references(
    execution: RepoBInitializationExecution,
    proof: DeployKeyAccessProof,
    key_directory: Path,
    work_directory: Path,
    known_hosts_file: Path,
    git_executable: Path,
    ssh_executable: Path,
) -> DeployKeyReferenceSnapshot:
    snapshot = read_repo_b_deploy_key_references(
        proof,
        execution.target,
        execution.repository_id,
        key_directory,
        known_hosts_file=known_hosts_file,
        work_directory=work_directory,
        git_executable=git_executable,
        ssh_executable=ssh_executable,
    )
    _validate_reference_snapshot(snapshot, execution)
    return snapshot


def _create_probe_directory(workspace_root: Path) -> Path:
    try:
        metadata = workspace_root.lstat()
        if (
            not workspace_root.is_absolute()
            or workspace_root.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            _invalid()
        trusted_root = workspace_root.resolve(strict=True)
        probe = Path(tempfile.mkdtemp(prefix=".repo-init-probe-", dir=trusted_root))
        os.chmod(probe, 0o700)
        return probe
    except RepoBInitializationExecutionError:
        raise
    except OSError:
        _invalid()


def _validate_reference_snapshot(
    snapshot: DeployKeyReferenceSnapshot,
    execution: RepoBInitializationExecution,
) -> None:
    if (
        type(snapshot) is not DeployKeyReferenceSnapshot
        or snapshot.target != execution.target
        or snapshot.repository_id != execution.repository_id
        or snapshot.key_fingerprint != execution.key_fingerprint
        or snapshot.generation_id != execution.generation_id
        or type(snapshot.references) is not tuple
        or _HEX_64.fullmatch(snapshot.observation_sha256) is None
    ):
        _invalid()
    raw = bytearray()
    previous: str | None = None
    for reference in snapshot.references:
        if (
            type(reference) is not DeployKeyReference
            or type(reference.name) is not str
            or not _valid_reference_name(reference.name)
            or type(reference.commit_sha) is not str
            or _COMMIT_SHA.fullmatch(reference.commit_sha) is None
            or (previous is not None and reference.name <= previous)
        ):
            _invalid()
        raw.extend(f"{reference.commit_sha}\t{reference.name}\n".encode("ascii"))
        previous = reference.name
    if hashlib.sha256(raw).hexdigest() != snapshot.observation_sha256:
        _invalid()


def _valid_reference_name(name: str) -> bool:
    if (
        len(name) > 1_024
        or not name.startswith(("refs/heads/", "refs/tags/"))
        or name.endswith(("/", ".", ".lock"))
        or ".." in name
        or "//" in name
        or "@{" in name
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
        or any(character in " ~^:?*[\\" for character in name)
    ):
        return False
    return all(component not in {"", ".", ".."} for component in name.split("/"))


def _is_exact_completed_remote(
    snapshot: DeployKeyReferenceSnapshot,
    execution: RepoBInitializationExecution,
) -> bool:
    return snapshot.references == (DeployKeyReference("refs/heads/main", execution.commit_sha),)


def _proof_matches_authority(proof: object, authority: RepoBInitializationAuthority) -> bool:
    return (
        type(proof) is DeployKeyAccessProof
        and proof.target == authority.target
        and proof.repository_id == authority.repository_id
        and proof.key_fingerprint == authority.key_fingerprint
        and proof.generation_id == authority.generation_id
        and proof.ref_count == 0
        and proof.observation_sha256 == authority.observation_sha256
        and _FINGERPRINT.fullmatch(proof.key_fingerprint) is not None
        and _HEX_64.fullmatch(proof.observation_sha256) is not None
    )


def _validate_execution_authority_binding(
    execution: RepoBInitializationExecution,
    authority: RepoBInitializationAuthority,
) -> None:
    if (
        execution.request_id != authority.request_id
        or execution.target != authority.target
        or execution.repository_id != authority.repository_id
        or execution.key_fingerprint != authority.key_fingerprint
        or execution.generation_id != authority.generation_id
    ):
        _invalid()


def _validate_terminal_replay(
    store: StateStore,
    execution: RepoBInitializationExecution,
    authority: RepoBInitializationAuthority,
) -> None:
    _validate_execution_authority_binding(execution, authority)
    if execution.phase == "completed":
        if authority.phase != "completed" or authority.block_reason != "none":
            _invalid()
        baseline = store.synchronization_baseline(execution.target, "main")
        if (
            baseline is None
            or baseline.snapshot_id != execution.snapshot_id
            or baseline.commit_sha != execution.commit_sha
        ):
            _invalid()
    elif (
        execution.phase != "blocked"
        or authority.phase != "blocked"
        or authority.block_reason != "execution_blocked"
    ):
        _invalid()


def _prepare(
    store: StateStore,
    authority: RepoBInitializationAuthority,
    snapshot_id: str,
    commit_sha: str,
    prepared_at: datetime,
) -> RepoBInitializationExecution:
    execution = _new_execution(
        authority.request_id,
        authority.target,
        authority.repository_id,
        authority.key_fingerprint,
        authority.generation_id,
        snapshot_id,
        commit_sha,
        "prepared",
        "none",
        0,
        prepared_at,
        prepared_at,
        None,
        None,
    )
    try:
        with store._connection as database:
            database.execute("BEGIN IMMEDIATE")
            if load_repo_b_initialization_execution(store, execution.request_id) is not None:
                _invalid()
            database.execute(
                "INSERT INTO repo_b_initialization_execution "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _execution_row(execution),
            )
    except sqlite3.Error:
        _invalid()
    persisted = load_repo_b_initialization_execution(store, execution.request_id)
    if persisted != execution:
        _invalid()
    return execution


def _transition(
    store: StateStore,
    current: RepoBInitializationExecution,
    *,
    phase: str,
    block_reason: str,
    attempt_count: int,
    updated_at: datetime,
    next_attempt_at: datetime | None,
    terminal_at: datetime | None,
) -> RepoBInitializationExecution:
    successor = _new_execution(
        current.request_id,
        current.target,
        current.repository_id,
        current.key_fingerprint,
        current.generation_id,
        current.snapshot_id,
        current.commit_sha,
        phase,
        block_reason,
        attempt_count,
        current.prepared_at,
        updated_at,
        next_attempt_at,
        terminal_at,
    )
    try:
        with store._connection as database:
            database.execute("BEGIN IMMEDIATE")
            persisted = load_repo_b_initialization_execution(store, current.request_id)
            if persisted != current:
                _invalid()
            database.execute(
                "UPDATE repo_b_initialization_execution SET phase=?,block_reason=?,"
                "attempt_count=?,updated_at=?,next_attempt_at=?,terminal_at=?,record_sha256=? "
                "WHERE request_id=? AND record_sha256=?",
                (
                    successor.phase,
                    successor.block_reason,
                    successor.attempt_count,
                    successor.updated_at.isoformat(),
                    _optional_time(successor.next_attempt_at),
                    _optional_time(successor.terminal_at),
                    successor.record_sha256,
                    successor.request_id,
                    current.record_sha256,
                ),
            )
            if database.execute("SELECT changes()").fetchone() != (1,):
                _invalid()
    except sqlite3.Error:
        _invalid()
    persisted = load_repo_b_initialization_execution(store, current.request_id)
    if persisted != successor:
        _invalid()
    return successor


def _transport_failure(
    store: StateStore,
    current: RepoBInitializationExecution,
    authority: RepoBInitializationAuthority,
    error: PublicationTransportError,
    when: datetime,
) -> RepoBInitializationExecution:
    if error.transient and current.attempt_count < MAX_INITIALIZATION_ATTEMPTS:
        retry_at = _retry_time(when, current.attempt_count)
        return _transition(
            store,
            current,
            phase="retry",
            block_reason="none",
            attempt_count=current.attempt_count,
            updated_at=when,
            next_attempt_at=retry_at,
            terminal_at=None,
        )
    reason = "attempts_exhausted" if error.transient else "transport_rejected"
    return _block(store, current, authority, reason, when)


def _access_failure(
    store: StateStore,
    current: RepoBInitializationExecution,
    authority: RepoBInitializationAuthority,
    error: DeployKeyAccessError,
    when: datetime,
) -> RepoBInitializationExecution:
    translated = PublicationTransportError(
        "initialization transport failed", transient=error.transient
    )
    return _transport_failure(store, current, authority, translated, when)


def _block(
    store: StateStore,
    current: RepoBInitializationExecution,
    authority: RepoBInitializationAuthority,
    reason: str,
    when: datetime,
) -> RepoBInitializationExecution:
    successor = _new_execution(
        current.request_id,
        current.target,
        current.repository_id,
        current.key_fingerprint,
        current.generation_id,
        current.snapshot_id,
        current.commit_sha,
        "blocked",
        reason,
        current.attempt_count,
        current.prepared_at,
        when,
        None,
        when,
    )
    _finish_atomically(store, current, successor, authority, completed=False)
    return successor


def _complete(
    store: StateStore,
    current: RepoBInitializationExecution,
    authority: RepoBInitializationAuthority,
    when: datetime,
) -> RepoBInitializationExecution:
    successor = _new_execution(
        current.request_id,
        current.target,
        current.repository_id,
        current.key_fingerprint,
        current.generation_id,
        current.snapshot_id,
        current.commit_sha,
        "completed",
        "none",
        current.attempt_count,
        current.prepared_at,
        when,
        None,
        when,
    )
    _finish_atomically(store, current, successor, authority, completed=True)
    return successor


def _finish_atomically(
    store: StateStore,
    current: RepoBInitializationExecution,
    successor: RepoBInitializationExecution,
    authority: RepoBInitializationAuthority,
    *,
    completed: bool,
) -> None:
    authority_phase = "completed" if completed else "blocked"
    authority_reason = "none" if completed else "execution_blocked"
    if successor.terminal_at is None:
        _invalid()
    authority_sha = _authority_digest(
        authority.request_id,
        authority.target,
        authority.repository_id,
        authority.key_fingerprint,
        authority.generation_id,
        authority.observation_sha256,
        authority.observed_at,
        authority_phase,
        authority_reason,
        authority.recorded_at,
        successor.updated_at,
    )
    try:
        with store._connection as database:
            database.execute("BEGIN IMMEDIATE")
            if load_repo_b_initialization_execution(store, current.request_id) != current:
                _invalid()
            authority_rows = database.execute(
                "SELECT request_id,target,repository_id,key_fingerprint,generation_id,"
                "observation_sha256,observed_at,phase,block_reason,recorded_at,"
                "terminal_at,record_sha256 FROM repo_b_initialization WHERE request_id=?",
                (current.request_id,),
            ).fetchall()
            if len(authority_rows) != 1 or _authority_from_row(authority_rows[0]) != authority:
                _invalid()
            if authority.phase != "authorized":
                _invalid()
            if completed:
                baseline = database.execute(
                    "SELECT snapshot_id,commit_sha FROM synchronization_baseline "
                    "WHERE target=? AND branch='main'",
                    (current.target,),
                ).fetchone()
                if baseline is None:
                    database.execute(
                        "INSERT INTO synchronization_baseline VALUES (?,?,?,?,?)",
                        (
                            current.target,
                            "main",
                            current.snapshot_id,
                            current.commit_sha,
                            successor.updated_at.isoformat(),
                        ),
                    )
                elif baseline != (current.snapshot_id, current.commit_sha):
                    _invalid()
            database.execute(
                "UPDATE repo_b_initialization_execution SET phase=?,block_reason=?,"
                "attempt_count=?,updated_at=?,next_attempt_at=NULL,terminal_at=?,"
                "record_sha256=? WHERE request_id=? AND record_sha256=?",
                (
                    successor.phase,
                    successor.block_reason,
                    successor.attempt_count,
                    successor.updated_at.isoformat(),
                    successor.terminal_at.isoformat(),
                    successor.record_sha256,
                    successor.request_id,
                    current.record_sha256,
                ),
            )
            if database.execute("SELECT changes()").fetchone() != (1,):
                _invalid()
            database.execute(
                "UPDATE repo_b_initialization SET phase=?,block_reason=?,terminal_at=?,"
                "record_sha256=? WHERE request_id=? AND record_sha256=?",
                (
                    authority_phase,
                    authority_reason,
                    successor.updated_at.isoformat(),
                    authority_sha,
                    authority.request_id,
                    authority.record_sha256,
                ),
            )
            if database.execute("SELECT changes()").fetchone() != (1,):
                _invalid()
    except sqlite3.Error:
        _invalid()
    if load_repo_b_initialization_execution(store, current.request_id) != successor:
        _invalid()


def _new_execution(
    request_id: str,
    target: str,
    repository_id: int,
    key_fingerprint: str,
    generation_id: str,
    snapshot_id: str,
    commit_sha: str,
    phase: str,
    block_reason: str,
    attempt_count: int,
    prepared_at: datetime,
    updated_at: datetime,
    next_attempt_at: datetime | None,
    terminal_at: datetime | None,
) -> RepoBInitializationExecution:
    digest = _execution_digest(
        request_id,
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        snapshot_id,
        commit_sha,
        phase,
        block_reason,
        attempt_count,
        prepared_at,
        updated_at,
        next_attempt_at,
        terminal_at,
    )
    return RepoBInitializationExecution(
        request_id,
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        snapshot_id,
        commit_sha,
        phase,
        block_reason,
        attempt_count,
        prepared_at,
        updated_at,
        next_attempt_at,
        terminal_at,
        digest,
    )


def _execution_from_row(row: tuple[object, ...]) -> RepoBInitializationExecution:
    if len(row) != 15:
        _invalid()
    (
        request_id,
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        snapshot_id,
        commit_sha,
        phase,
        block_reason,
        attempt_count,
        prepared_at,
        updated_at,
        next_attempt_at,
        terminal_at,
        record_sha256,
    ) = row
    if type(request_id) is not str or type(generation_id) is not str:
        _invalid()
    try:
        canonical_request = str(UUID(request_id)) == request_id
        canonical_generation = str(UUID(generation_id)) == generation_id
    except (ValueError, AttributeError):
        _invalid()
    if (
        not canonical_request
        or type(target) is not str
        or _TARGET.fullmatch(target) is None
        or target.split("/", 1)[1] in {".", ".."}
        or type(repository_id) is not int
        or repository_id <= 0
        or type(key_fingerprint) is not str
        or _FINGERPRINT.fullmatch(key_fingerprint) is None
        or not canonical_generation
        or type(snapshot_id) is not str
        or _HEX_64.fullmatch(snapshot_id) is None
        or type(commit_sha) is not str
        or _COMMIT_SHA.fullmatch(commit_sha) is None
        or phase not in _PHASES
        or block_reason not in _BLOCK_REASONS
        or type(attempt_count) is not int
        or not 0 <= attempt_count <= MAX_INITIALIZATION_ATTEMPTS
        or type(record_sha256) is not str
        or _HEX_64.fullmatch(record_sha256) is None
    ):
        _invalid()
    prepared = _stored_time(prepared_at)
    updated = _stored_time(updated_at)
    retry = None if next_attempt_at is None else _stored_time(next_attempt_at)
    terminal = None if terminal_at is None else _stored_time(terminal_at)
    if updated < prepared or (terminal is not None and terminal < updated):
        _invalid()
    if (
        (
            phase == "prepared"
            and not (
                block_reason == "none" and attempt_count == 0 and retry is None and terminal is None
            )
        )
        or (
            phase == "publishing"
            and not (
                block_reason == "none" and attempt_count > 0 and retry is None and terminal is None
            )
        )
        or (
            phase == "retry"
            and not (
                block_reason == "none"
                and attempt_count > 0
                and retry is not None
                and terminal is None
                and retry > updated
            )
        )
        or (
            phase == "completed"
            and not (
                block_reason == "none"
                and attempt_count > 0
                and retry is None
                and terminal is not None
            )
        )
        or (
            phase == "blocked"
            and not (block_reason != "none" and retry is None and terminal is not None)
        )
    ):
        _invalid()
    expected = _execution_digest(
        request_id,
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        snapshot_id,
        commit_sha,
        phase,
        block_reason,
        attempt_count,
        prepared,
        updated,
        retry,
        terminal,
    )
    if expected != record_sha256:
        _invalid()
    return RepoBInitializationExecution(
        request_id,
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        snapshot_id,
        commit_sha,
        phase,
        block_reason,
        attempt_count,
        prepared,
        updated,
        retry,
        terminal,
        record_sha256,
    )


def _execution_row(execution: RepoBInitializationExecution) -> tuple[object, ...]:
    return (
        execution.request_id,
        execution.target,
        execution.repository_id,
        execution.key_fingerprint,
        execution.generation_id,
        execution.snapshot_id,
        execution.commit_sha,
        execution.phase,
        execution.block_reason,
        execution.attempt_count,
        execution.prepared_at.isoformat(),
        execution.updated_at.isoformat(),
        _optional_time(execution.next_attempt_at),
        _optional_time(execution.terminal_at),
        execution.record_sha256,
    )


def _execution_digest(
    request_id: str,
    target: str,
    repository_id: int,
    key_fingerprint: str,
    generation_id: str,
    snapshot_id: str,
    commit_sha: str,
    phase: str,
    block_reason: str,
    attempt_count: int,
    prepared_at: datetime,
    updated_at: datetime,
    next_attempt_at: datetime | None,
    terminal_at: datetime | None,
) -> str:
    raw = json.dumps(
        [
            1,
            request_id,
            target,
            repository_id,
            key_fingerprint,
            generation_id,
            snapshot_id,
            commit_sha,
            phase,
            block_reason,
            attempt_count,
            prepared_at.isoformat(),
            updated_at.isoformat(),
            _optional_time(next_attempt_at),
            _optional_time(terminal_at),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _stored_time(value: object) -> datetime:
    parsed = _parse_timestamp(value)
    if type(value) is not str or parsed.isoformat() != value:
        _invalid()
    return parsed


def _canonical_time(value: datetime | None) -> datetime:
    if value is not None and (type(value) is not datetime or value.utcoffset() != timedelta(0)):
        _invalid()
    return _timestamp(value)


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _invalid() -> NoReturn:
    raise RepoBInitializationExecutionError(
        "Repo B initialization execution failed closed"
    ) from None
