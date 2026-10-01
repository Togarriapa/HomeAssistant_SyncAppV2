"""Durable one-shot operator controls for the Repo B deploy-key lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from .deploy_key import DeployKeyEnrollment, DeployKeyError, ensure_repo_b_deploy_key
from .deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    read_repo_b_deploy_key_references,
    test_repo_b_deploy_key_access,
)
from .deploy_key_rotation import (
    DeployKeyRotationError,
    DeployKeyRotationStatus,
    activate_repo_b_deploy_key_rotation,
    prepare_repo_b_deploy_key_rotation,
    verify_repo_b_deploy_key_rotation,
)
from .repo_initialization import (
    RepoBInitializationError,
    RepoBInitializationRequest,
    authorize_repo_b_initialization,
)
from .repo_initialization_execution import (
    RepoBInitializationExecutionError,
    execute_authorized_repo_b_initialization,
)
from .state import StateError, StateStore, _parse_timestamp, _timestamp

ACTIONS = frozenset(
    {"generate", "test", "rotate_prepare", "rotate_verify", "rotate_activate", "initialize"}
)
MAX_ATTEMPTS = 8
_TARGET = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9._-]{1,100}"
)
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


class DeployKeyAdministrationError(RuntimeError):
    """An operator request or its durable receipt failed closed."""


@dataclass(frozen=True, slots=True)
class DeployKeyAdministrativeRequest:
    """One explicit, UUID-bound operator action."""

    request_id: str
    action: str


@dataclass(frozen=True, slots=True)
class DeployKeyAdministrativeReceipt:
    """Integrity-protected receipt containing enrollment-safe metadata only."""

    request_id: str
    action: str
    target_sha256: str
    repository_id: int
    status: str
    outcome: str
    attempts: int
    next_attempt_at: datetime | None
    processed_at: datetime
    public_key: str | None
    fingerprint: str | None
    generation_id: str | None
    record_sha256: str


@dataclass(frozen=True, slots=True)
class DeployKeyAdministrativeResult:
    """Sanitized startup result; private key bytes and credentials are impossible fields."""

    outcome: str
    replayed: bool
    public_key: str | None = None
    fingerprint: str | None = None
    generation_id: str | None = None


def apply_deploy_key_administrative_request(
    store: StateStore,
    request: DeployKeyAdministrativeRequest,
    *,
    target: str,
    repository_id: int,
    github_token: str,
    key_directory: Path,
    work_directory: Path,
    source: Path,
    snapshot_root: Path,
    workspace_root: Path,
    now: datetime | None = None,
) -> DeployKeyAdministrativeResult:
    """Apply or replay one exact action with durable blocking and retry backoff."""

    if type(store) is not StateStore or type(request) is not DeployKeyAdministrativeRequest:
        _invalid()
    try:
        _validate_request(request, target, repository_id, github_token)
        when = _timestamp(now)
        target_sha256 = hashlib.sha256(target.casefold().encode("ascii")).hexdigest()
        existing = _load_receipt(store._connection, request.request_id, request.action)
        attempts = 1
        if existing is not None:
            if existing.target_sha256 != target_sha256 or existing.repository_id != repository_id:
                _invalid()
            if existing.status != "retry" or (
                existing.next_attempt_at is not None and when < existing.next_attempt_at
            ):
                return _result(existing, replayed=True)
            attempts = existing.attempts + 1

        try:
            outcome, public_key, fingerprint, generation_id = _perform(
                store,
                request,
                target,
                repository_id,
                github_token,
                key_directory,
                work_directory,
                source,
                snapshot_root,
                workspace_root,
                when,
            )
            status = "blocked" if outcome == "blocked" else "completed"
            next_attempt_at = None
        except (
            DeployKeyError,
            DeployKeyAccessError,
            DeployKeyRotationError,
            RepoBInitializationError,
            RepoBInitializationExecutionError,
            OSError,
        ) as error:
            transient = bool(getattr(error, "transient", False)) or isinstance(error, OSError)
            exhausted = attempts >= MAX_ATTEMPTS
            status = "retry" if transient and not exhausted else "blocked"
            outcome = "retry" if status == "retry" else "blocked"
            next_attempt_at = (
                when + timedelta(seconds=min(60 * (2 ** (attempts - 1)), 3600))
                if status == "retry"
                else None
            )
            public_key = fingerprint = generation_id = None

        receipt = _new_receipt(
            request,
            target_sha256,
            repository_id,
            status,
            outcome,
            attempts,
            next_attempt_at,
            when,
            public_key,
            fingerprint,
            generation_id,
        )
        _save_receipt(store._connection, receipt)
        return _result(receipt, replayed=False)
    except DeployKeyAdministrationError:
        raise
    except (StateError, sqlite3.Error, TypeError, ValueError, AttributeError, OverflowError):
        _invalid()


def load_deploy_key_administrative_receipt(
    store: StateStore,
    request_id: str,
    action: str,
) -> DeployKeyAdministrativeReceipt | None:
    """Load and integrity-check one exact operator receipt."""

    if type(store) is not StateStore:
        _invalid()
    try:
        _validate_request_id(request_id)
        if action not in ACTIONS:
            _invalid()
        return _load_receipt(store._connection, request_id, action)
    except DeployKeyAdministrationError:
        raise
    except (StateError, sqlite3.Error, TypeError, ValueError, AttributeError):
        _invalid()


def _perform(
    store: StateStore,
    request: DeployKeyAdministrativeRequest,
    target: str,
    repository_id: int,
    github_token: str,
    key_directory: Path,
    work_directory: Path,
    source: Path,
    snapshot_root: Path,
    workspace_root: Path,
    when: datetime,
) -> tuple[str, str | None, str | None, str | None]:
    if request.action == "generate":
        return _enrollment_result("generated", ensure_repo_b_deploy_key(key_directory))
    if request.action == "test":
        proof = test_repo_b_deploy_key_access(
            target,
            github_token,
            repository_id,
            key_directory,
            work_directory=work_directory,
        )
        return _proof_result("tested", proof)
    if request.action == "rotate_prepare":
        status = prepare_repo_b_deploy_key_rotation(
            key_directory, request.request_id, target, repository_id
        )
        return _rotation_result("prepared", status)
    if request.action == "rotate_verify":
        status = verify_repo_b_deploy_key_rotation(
            key_directory,
            request.request_id,
            target,
            github_token,
            repository_id,
            work_directory=work_directory,
        )
        return _rotation_result("verified", status)
    if request.action == "rotate_activate":
        status = activate_repo_b_deploy_key_rotation(key_directory, request.request_id)
        return _rotation_result("activated", status)

    proof = test_repo_b_deploy_key_access(
        target,
        github_token,
        repository_id,
        key_directory,
        work_directory=work_directory,
    )
    snapshot = read_repo_b_deploy_key_references(
        proof,
        target,
        repository_id,
        key_directory,
        work_directory=work_directory,
    )
    authority = authorize_repo_b_initialization(
        store,
        RepoBInitializationRequest(request.request_id, target, repository_id),
        proof,
        snapshot,
        observed_at=when,
        now=when,
    )
    if authority.phase == "blocked":
        return "blocked", None, proof.key_fingerprint, proof.generation_id
    execution = execute_authorized_repo_b_initialization(
        store,
        request.request_id,
        source,
        snapshot_root,
        workspace_root,
        proof,
        key_directory,
        now=when,
    )
    if execution.phase == "blocked":
        return "blocked", None, proof.key_fingerprint, proof.generation_id
    if execution.phase == "retry":
        raise OSError("initialization retry remains pending")
    if execution.phase != "completed":
        raise RepoBInitializationExecutionError(
            "initialization did not reach a safe terminal state"
        )
    return "initialized", None, proof.key_fingerprint, proof.generation_id


def _enrollment_result(
    outcome: str, enrollment: DeployKeyEnrollment
) -> tuple[str, str | None, str | None, str | None]:
    return outcome, enrollment.public_key, enrollment.fingerprint, enrollment.generation_id


def _proof_result(
    outcome: str, proof: DeployKeyAccessProof
) -> tuple[str, str | None, str | None, str | None]:
    return outcome, None, proof.key_fingerprint, proof.generation_id


def _rotation_result(
    outcome: str, status: DeployKeyRotationStatus
) -> tuple[str, str | None, str | None, str | None]:
    public_key = status.candidate_public_key if outcome == "prepared" else None
    fingerprint = status.candidate_fingerprint or status.active_fingerprint
    generation_id = status.candidate_generation_id or status.active_generation_id
    return outcome, public_key, fingerprint, generation_id


def _save_receipt(database: sqlite3.Connection, receipt: DeployKeyAdministrativeReceipt) -> None:
    values = _receipt_values(receipt)
    with database:
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            "INSERT INTO deploy_key_administrative_request "
            "(request_id,action,target_sha256,repository_id,status,outcome,attempts,"
            "next_attempt_at,processed_at,public_key,fingerprint,generation_id,record_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id,action) DO UPDATE SET "
            "target_sha256=excluded.target_sha256,repository_id=excluded.repository_id,"
            "status=excluded.status,outcome=excluded.outcome,attempts=excluded.attempts,"
            "next_attempt_at=excluded.next_attempt_at,processed_at=excluded.processed_at,"
            "public_key=excluded.public_key,fingerprint=excluded.fingerprint,"
            "generation_id=excluded.generation_id,record_sha256=excluded.record_sha256",
            values,
        )
        if _load_receipt(database, receipt.request_id, receipt.action) != receipt:
            _invalid()


def _load_receipt(
    database: sqlite3.Connection, request_id: str, action: str
) -> DeployKeyAdministrativeReceipt | None:
    rows = database.execute(
        "SELECT request_id,action,target_sha256,repository_id,status,outcome,attempts,"
        "next_attempt_at,processed_at,public_key,fingerprint,generation_id,record_sha256 "
        "FROM deploy_key_administrative_request WHERE request_id=? AND action=?",
        (request_id, action),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 13:
        _invalid()
    row = rows[0]
    (
        stored_id,
        stored_action,
        target_sha256,
        repository_id,
        status,
        outcome,
        attempts,
        next_attempt_at,
        processed_at,
        public_key,
        fingerprint,
        generation_id,
        record_sha256,
    ) = row
    _validate_request_id(stored_id)
    if (
        stored_action not in ACTIONS
        or type(target_sha256) is not str
        or _HEX_64.fullmatch(target_sha256) is None
        or type(repository_id) is not int
        or repository_id <= 0
        or status not in {"completed", "retry", "blocked"}
        or type(outcome) is not str
        or not outcome
        or type(attempts) is not int
        or not 1 <= attempts <= MAX_ATTEMPTS
        or type(record_sha256) is not str
        or _HEX_64.fullmatch(record_sha256) is None
    ):
        _invalid()
    next_when = None if next_attempt_at is None else _parse_timestamp(next_attempt_at)
    processed_when = _parse_timestamp(processed_at)
    if (status == "retry") != (next_when is not None):
        _invalid()
    _validate_safe_metadata(public_key, fingerprint, generation_id)
    receipt = DeployKeyAdministrativeReceipt(
        stored_id,
        stored_action,
        target_sha256,
        repository_id,
        status,
        outcome,
        attempts,
        next_when,
        processed_when,
        public_key,
        fingerprint,
        generation_id,
        record_sha256,
    )
    if receipt.record_sha256 != _receipt_digest(receipt):
        _invalid()
    return receipt


def _new_receipt(
    request: DeployKeyAdministrativeRequest,
    target_sha256: str,
    repository_id: int,
    status: str,
    outcome: str,
    attempts: int,
    next_attempt_at: datetime | None,
    processed_at: datetime,
    public_key: str | None,
    fingerprint: str | None,
    generation_id: str | None,
) -> DeployKeyAdministrativeReceipt:
    unsigned = DeployKeyAdministrativeReceipt(
        request.request_id,
        request.action,
        target_sha256,
        repository_id,
        status,
        outcome,
        attempts,
        next_attempt_at,
        processed_at,
        public_key,
        fingerprint,
        generation_id,
        "",
    )
    return DeployKeyAdministrativeReceipt(
        unsigned.request_id,
        unsigned.action,
        unsigned.target_sha256,
        unsigned.repository_id,
        unsigned.status,
        unsigned.outcome,
        unsigned.attempts,
        unsigned.next_attempt_at,
        unsigned.processed_at,
        unsigned.public_key,
        unsigned.fingerprint,
        unsigned.generation_id,
        _receipt_digest(unsigned),
    )


def _receipt_values(receipt: DeployKeyAdministrativeReceipt) -> tuple[object, ...]:
    return (
        receipt.request_id,
        receipt.action,
        receipt.target_sha256,
        receipt.repository_id,
        receipt.status,
        receipt.outcome,
        receipt.attempts,
        None if receipt.next_attempt_at is None else receipt.next_attempt_at.isoformat(),
        receipt.processed_at.isoformat(),
        receipt.public_key,
        receipt.fingerprint,
        receipt.generation_id,
        receipt.record_sha256,
    )


def _receipt_digest(receipt: DeployKeyAdministrativeReceipt) -> str:
    payload = [1, *_receipt_values(receipt)[:-1]]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _result(
    receipt: DeployKeyAdministrativeReceipt, *, replayed: bool
) -> DeployKeyAdministrativeResult:
    return DeployKeyAdministrativeResult(
        receipt.outcome,
        replayed,
        receipt.public_key,
        receipt.fingerprint,
        receipt.generation_id,
    )


def _validate_request(
    request: DeployKeyAdministrativeRequest,
    target: object,
    repository_id: object,
    github_token: object,
) -> None:
    _validate_request_id(request.request_id)
    if (
        request.action not in ACTIONS
        or type(target) is not str
        or _TARGET.fullmatch(target) is None
        or target.split("/", 1)[1] in {".", ".."}
        or type(repository_id) is not int
        or repository_id <= 0
        or type(github_token) is not str
        or not github_token
    ):
        _invalid()


def _validate_request_id(value: object) -> None:
    if type(value) is not str:
        _invalid()
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        _invalid()


def _validate_safe_metadata(public_key: object, fingerprint: object, generation_id: object) -> None:
    if public_key is not None and (
        type(public_key) is not str
        or len(public_key) > 4096
        or not public_key.startswith("ssh-ed25519 ")
        or "\n" in public_key
    ):
        _invalid()
    if fingerprint is not None and (
        type(fingerprint) is not str or _FINGERPRINT.fullmatch(fingerprint) is None
    ):
        _invalid()
    if generation_id is not None:
        _validate_request_id(generation_id)


def _invalid() -> NoReturn:
    raise DeployKeyAdministrationError("deploy-key administrative request failed closed") from None
