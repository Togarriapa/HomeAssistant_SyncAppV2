"""Explicit, crash-safe authority to initialize one proven-empty private Repo B."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn
from uuid import UUID

from .deploy_key_access import (
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
)
from .state import StateError, StateStore, _parse_timestamp, _timestamp

MAX_AUTHORIZED_INITIALIZATIONS = 16
MAX_OBSERVATION_AGE = timedelta(minutes=5)
MAX_CLOCK_SKEW = timedelta(seconds=30)
_TARGET = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9._-]{1,100}"
)
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_PHASES = {"authorized", "blocked", "completed"}
_BLOCK_REASONS = {
    "none",
    "repository_not_empty",
    "already_initialized",
    "active_request",
}


class RepoBInitializationError(RuntimeError):
    """Repo B initialization authority failed closed."""


@dataclass(frozen=True, slots=True)
class RepoBInitializationRequest:
    """One explicit operator request bound to an exact repository identity."""

    request_id: str
    target: str
    repository_id: int


@dataclass(frozen=True, slots=True)
class RepoBInitializationAuthority:
    """Content-free durable authorization or deterministic block receipt."""

    request_id: str
    target: str
    repository_id: int
    key_fingerprint: str
    generation_id: str
    observation_sha256: str
    observed_at: datetime
    phase: str
    block_reason: str
    recorded_at: datetime
    terminal_at: datetime | None
    record_sha256: str


def authorize_repo_b_initialization(
    store: StateStore,
    request: RepoBInitializationRequest,
    proof: DeployKeyAccessProof | None = None,
    snapshot: DeployKeyReferenceSnapshot | None = None,
    *,
    observed_at: datetime | None = None,
    now: datetime | None = None,
) -> RepoBInitializationAuthority:
    """Atomically authorize a proven-empty repo, or durably record why it was blocked.

    An exact request may be replayed without remote evidence. A previously unseen
    request requires a fresh, canonical deploy-key observation and never performs
    network or Git mutation itself.
    """

    if type(store) is not StateStore or type(request) is not RepoBInitializationRequest:
        _invalid()
    try:
        _validate_request(request)
        recorded_at = _canonical_time(now)
        with store._connection as database:
            database.execute("BEGIN IMMEDIATE")
            existing = _load_authority(database, request.request_id)
            if existing is not None:
                if (
                    existing.target != request.target
                    or existing.repository_id != request.repository_id
                ):
                    _invalid()
                if any(value is not None for value in (proof, snapshot, observed_at)):
                    if proof is None or snapshot is None or observed_at is None:
                        _invalid()
                    observed = _validate_evidence(
                        request,
                        proof,
                        snapshot,
                        observed_at,
                        recorded_at,
                    )
                    if (
                        existing.key_fingerprint != proof.key_fingerprint
                        or existing.generation_id != proof.generation_id
                        or existing.observation_sha256 != proof.observation_sha256
                        or existing.observed_at != observed
                    ):
                        _invalid()
                return existing

            if proof is None or snapshot is None or observed_at is None:
                _invalid()
            observed = _validate_evidence(request, proof, snapshot, observed_at, recorded_at)
            pinned = database.execute(
                "SELECT repository_id FROM repository_binding WHERE target=?",
                (request.target,),
            ).fetchall()
            if len(pinned) != 1 or pinned[0] != (request.repository_id,):
                _invalid()

            baselines = database.execute(
                "SELECT COUNT(*) FROM synchronization_baseline WHERE target=?",
                (request.target,),
            ).fetchone()
            if baselines is None or len(baselines) != 1 or type(baselines[0]) is not int:
                _invalid()
            active = database.execute(
                "SELECT COUNT(*) FROM repo_b_initialization WHERE target=? AND phase='authorized'",
                (request.target,),
            ).fetchone()
            if active is None or len(active) != 1 or type(active[0]) is not int:
                _invalid()
            if baselines[0] > 0:
                phase, reason = "blocked", "already_initialized"
            elif snapshot.references:
                phase, reason = "blocked", "repository_not_empty"
            elif active[0] == 1:
                phase, reason = "blocked", "active_request"
            elif active[0] != 0:
                _invalid()
            else:
                phase, reason = "authorized", "none"

            terminal_at = recorded_at if phase == "blocked" else None
            authority = _new_authority(
                request,
                proof,
                observed,
                phase,
                reason,
                recorded_at,
                terminal_at,
            )
            database.execute(
                "INSERT INTO repo_b_initialization "
                "(request_id,target,repository_id,key_fingerprint,generation_id,"
                "observation_sha256,observed_at,phase,block_reason,recorded_at,"
                "terminal_at,record_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                _authority_row(authority),
            )
            persisted = _load_authority(database, request.request_id)
            if persisted != authority:
                _invalid()
        return authority
    except RepoBInitializationError:
        raise
    except (StateError, sqlite3.Error, TypeError, ValueError, AttributeError):
        _invalid()


def load_repo_b_initialization(
    store: StateStore,
    request_id: str,
) -> RepoBInitializationAuthority | None:
    """Load and integrity-check one initialization authority record."""

    if type(store) is not StateStore:
        _invalid()
    try:
        _validate_request_id(request_id)
        return _load_authority(store._connection, request_id)
    except RepoBInitializationError:
        raise
    except (StateError, sqlite3.Error, TypeError, ValueError, AttributeError):
        _invalid()


def discover_authorized_repo_b_initializations(
    store: StateStore,
) -> tuple[RepoBInitializationAuthority, ...]:
    """Discover bounded authorized work for a future idempotent executor."""

    if type(store) is not StateStore:
        _invalid()
    try:
        rows = store._connection.execute(
            "SELECT request_id,target,repository_id,key_fingerprint,generation_id,"
            "observation_sha256,observed_at,phase,block_reason,recorded_at,terminal_at,"
            "record_sha256 FROM repo_b_initialization WHERE phase='authorized' "
            "ORDER BY recorded_at, request_id LIMIT ?",
            (MAX_AUTHORIZED_INITIALIZATIONS + 1,),
        ).fetchall()
        if len(rows) > MAX_AUTHORIZED_INITIALIZATIONS:
            _invalid()
        return tuple(_authority_from_row(row) for row in rows)
    except RepoBInitializationError:
        raise
    except (StateError, sqlite3.Error, TypeError, ValueError, AttributeError):
        _invalid()


def _validate_request(request: RepoBInitializationRequest) -> None:
    _validate_request_id(request.request_id)
    if (
        type(request.target) is not str
        or _TARGET.fullmatch(request.target) is None
        or request.target.split("/", 1)[1] in {".", ".."}
        or type(request.repository_id) is not int
        or request.repository_id <= 0
    ):
        _invalid()


def _validate_request_id(value: object) -> None:
    if type(value) is not str:
        _invalid()
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        _invalid()


def _validate_evidence(
    request: RepoBInitializationRequest,
    proof: DeployKeyAccessProof,
    snapshot: DeployKeyReferenceSnapshot,
    observed_at: datetime,
    recorded_at: datetime,
) -> datetime:
    if type(proof) is not DeployKeyAccessProof or type(snapshot) is not DeployKeyReferenceSnapshot:
        _invalid()
    generation = UUID(proof.generation_id)
    if (
        proof.target != request.target
        or proof.repository_id != request.repository_id
        or snapshot.target != request.target
        or snapshot.repository_id != request.repository_id
        or type(proof.key_fingerprint) is not str
        or _FINGERPRINT.fullmatch(proof.key_fingerprint) is None
        or str(generation) != proof.generation_id
        or snapshot.key_fingerprint != proof.key_fingerprint
        or snapshot.generation_id != proof.generation_id
        or type(proof.ref_count) is not int
        or proof.ref_count < 0
        or type(proof.observation_sha256) is not str
        or _HEX_64.fullmatch(proof.observation_sha256) is None
        or snapshot.observation_sha256 != proof.observation_sha256
        or type(snapshot.references) is not tuple
        or proof.ref_count != len(snapshot.references)
    ):
        _invalid()
    raw = _canonical_reference_bytes(snapshot.references)
    if hashlib.sha256(raw).hexdigest() != proof.observation_sha256:
        _invalid()
    observed = _canonical_time(observed_at)
    if observed < recorded_at - MAX_OBSERVATION_AGE or observed > recorded_at + MAX_CLOCK_SKEW:
        _invalid()
    return observed


def _canonical_reference_bytes(references: tuple[DeployKeyReference, ...]) -> bytes:
    raw = bytearray()
    previous: str | None = None
    for reference in references:
        if type(reference) is not DeployKeyReference:
            _invalid()
        if (
            type(reference.name) is not str
            or type(reference.commit_sha) is not str
            or not _valid_reference(reference.name)
            or _COMMIT_SHA.fullmatch(reference.commit_sha) is None
            or (previous is not None and reference.name <= previous)
        ):
            _invalid()
        raw.extend(f"{reference.commit_sha}\t{reference.name}\n".encode("ascii"))
        previous = reference.name
    return bytes(raw)


def _valid_reference(reference: str) -> bool:
    if len(reference) > 1_024 or not reference.startswith(("refs/heads/", "refs/tags/")):
        return False
    if (
        reference.endswith(("/", ".", ".lock"))
        or ".." in reference
        or "//" in reference
        or "@{" in reference
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in reference)
        or any(character in " ~^:?*[\\" for character in reference)
    ):
        return False
    return all(component not in {"", ".", ".."} for component in reference.split("/"))


def _canonical_time(value: datetime | None) -> datetime:
    if value is not None and type(value) is not datetime:
        _invalid()
    parsed = _timestamp(value)
    if value is not None and value.utcoffset() != timedelta(0):
        _invalid()
    return parsed


def _new_authority(
    request: RepoBInitializationRequest,
    proof: DeployKeyAccessProof,
    observed_at: datetime,
    phase: str,
    block_reason: str,
    recorded_at: datetime,
    terminal_at: datetime | None,
) -> RepoBInitializationAuthority:
    record_sha256 = _record_digest(
        request.request_id,
        request.target,
        request.repository_id,
        proof.key_fingerprint,
        proof.generation_id,
        proof.observation_sha256,
        observed_at,
        phase,
        block_reason,
        recorded_at,
        terminal_at,
    )
    return RepoBInitializationAuthority(
        request.request_id,
        request.target,
        request.repository_id,
        proof.key_fingerprint,
        proof.generation_id,
        proof.observation_sha256,
        observed_at,
        phase,
        block_reason,
        recorded_at,
        terminal_at,
        record_sha256,
    )


def _load_authority(
    database: sqlite3.Connection,
    request_id: str,
) -> RepoBInitializationAuthority | None:
    rows = database.execute(
        "SELECT request_id,target,repository_id,key_fingerprint,generation_id,"
        "observation_sha256,observed_at,phase,block_reason,recorded_at,terminal_at,"
        "record_sha256 FROM repo_b_initialization WHERE request_id=?",
        (request_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        _invalid()
    return _authority_from_row(rows[0])


def _authority_from_row(row: tuple[object, ...]) -> RepoBInitializationAuthority:
    if len(row) != 12:
        _invalid()
    (
        request_id,
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        observation_sha256,
        observed_at,
        phase,
        block_reason,
        recorded_at,
        terminal_at,
        record_sha256,
    ) = row
    if type(request_id) is not str or type(target) is not str or type(repository_id) is not int:
        _invalid()
    request = RepoBInitializationRequest(request_id, target, repository_id)
    _validate_request(request)
    if (
        type(key_fingerprint) is not str
        or _FINGERPRINT.fullmatch(key_fingerprint) is None
        or type(generation_id) is not str
        or str(UUID(generation_id)) != generation_id
        or type(observation_sha256) is not str
        or _HEX_64.fullmatch(observation_sha256) is None
        or phase not in _PHASES
        or block_reason not in _BLOCK_REASONS
        or type(record_sha256) is not str
        or _HEX_64.fullmatch(record_sha256) is None
    ):
        _invalid()
    observed = _canonical_stored_time(observed_at)
    recorded = _canonical_stored_time(recorded_at)
    terminal = None if terminal_at is None else _canonical_stored_time(terminal_at)
    if (
        (phase == "authorized" and (block_reason != "none" or terminal is not None))
        or (phase == "blocked" and (block_reason == "none" or terminal is None))
        or (phase == "completed" and (block_reason != "none" or terminal is None))
        or (terminal is not None and terminal < recorded)
    ):
        _invalid()
    expected = _record_digest(
        request_id,
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        observation_sha256,
        observed,
        phase,
        block_reason,
        recorded,
        terminal,
    )
    if expected != record_sha256:
        _invalid()
    return RepoBInitializationAuthority(
        request_id,
        target,
        repository_id,
        key_fingerprint,
        generation_id,
        observation_sha256,
        observed,
        phase,
        block_reason,
        recorded,
        terminal,
        record_sha256,
    )


def _canonical_stored_time(value: object) -> datetime:
    parsed = _parse_timestamp(value)
    if type(value) is not str or parsed.isoformat() != value:
        _invalid()
    return parsed


def _authority_row(authority: RepoBInitializationAuthority) -> tuple[object, ...]:
    return (
        authority.request_id,
        authority.target,
        authority.repository_id,
        authority.key_fingerprint,
        authority.generation_id,
        authority.observation_sha256,
        authority.observed_at.isoformat(),
        authority.phase,
        authority.block_reason,
        authority.recorded_at.isoformat(),
        None if authority.terminal_at is None else authority.terminal_at.isoformat(),
        authority.record_sha256,
    )


def _record_digest(
    request_id: str,
    target: str,
    repository_id: int,
    key_fingerprint: str,
    generation_id: str,
    observation_sha256: str,
    observed_at: datetime,
    phase: str,
    block_reason: str,
    recorded_at: datetime,
    terminal_at: datetime | None,
) -> str:
    encoded = json.dumps(
        [
            1,
            request_id,
            target,
            repository_id,
            key_fingerprint,
            generation_id,
            observation_sha256,
            observed_at.isoformat(),
            phase,
            block_reason,
            recorded_at.isoformat(),
            None if terminal_at is None else terminal_at.isoformat(),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _invalid() -> NoReturn:
    raise RepoBInitializationError("Repo B initialization authority failed closed") from None
