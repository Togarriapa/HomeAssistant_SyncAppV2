"""Immutable authorization boundary for crash-safe deployment rollback."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .deployment_finalization import (
    DeploymentFinalization,
    DeploymentFinalizationError,
    is_candidate_blocked_by_finalization,
    load_deployment_finalization,
)
from .post_deployment_assertion_observation import (
    PostDeploymentAssertionObservationError,
    PostDeploymentAssertionPlan,
)
from .prepared_deployment import PreparedDeployment, PreparedDeploymentError, validate_deployment_id
from .state import StateError, StateStore

_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CORE = re.compile(r"^20[0-9]{2}\.(?:[1-9]|1[0-2])\.(?:0|[1-9][0-9]*)$")
_TOKEN = re.compile(r"^[!-~]{1,512}$")


class DeploymentRollbackError(RuntimeError):
    """Rollback authority or durable state is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class RollbackRepositoryProof:
    repository_id: int
    private: bool
    main_sha: str

    def validate(self) -> None:
        if (
            type(self.repository_id) is not int
            or self.repository_id <= 0
            or type(self.private) is not bool
            or _COMMIT.fullmatch(self.main_sha) is None
        ):
            _invalid_proof("repository")


@dataclass(frozen=True, slots=True)
class RollbackBackupProof:
    slug: str
    backup_type: str
    homeassistant_version: str
    includes_homeassistant: bool
    restorable: bool

    def validate(self) -> None:
        if (
            _SLUG.fullmatch(self.slug) is None
            or self.backup_type not in {"full", "partial"}
            or _CORE.fullmatch(self.homeassistant_version) is None
            or type(self.includes_homeassistant) is not bool
            or type(self.restorable) is not bool
        ):
            _invalid_proof("backup")


@dataclass(frozen=True, slots=True)
class DeploymentRollback:
    deployment_id: str
    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    backup_slug: str
    finalization_sha256: str
    repository_proof_sha256: str
    backup_proof_sha256: str
    phase: str
    reconciliation_state: str
    block_reason: str
    attempt_count: int
    authorized_at: datetime
    updated_at: datetime
    record_sha256: str

    @classmethod
    def create(
        cls,
        *,
        prepared: PreparedDeployment,
        finalization: DeploymentFinalization,
        repository_proof: RollbackRepositoryProof,
        backup_proof: RollbackBackupProof,
        authorized_at: datetime,
    ) -> DeploymentRollback:
        when = _timestamp(authorized_at)
        values: tuple[object, ...] = (
            prepared.deployment_id,
            prepared.evidence.target,
            prepared.evidence.repository_id,
            prepared.evidence.baseline_sha,
            prepared.evidence.candidate_sha,
            prepared.evidence.backup_slug,
            finalization.record_sha256,
            _repository_proof_digest(repository_proof),
            _backup_proof_digest(backup_proof),
            "planned",
            "none",
            "none",
            0,
            when.isoformat(),
            when.isoformat(),
        )
        result = cls(
            prepared.deployment_id,
            prepared.evidence.target,
            prepared.evidence.repository_id,
            prepared.evidence.baseline_sha,
            prepared.evidence.candidate_sha,
            prepared.evidence.backup_slug,
            finalization.record_sha256,
            _repository_proof_digest(repository_proof),
            _backup_proof_digest(backup_proof),
            "planned",
            "none",
            "none",
            0,
            when,
            when,
            _digest(values),
        )
        result.validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> DeploymentRollback:
        if len(row) != 16 or type(row[2]) is not int or type(row[12]) is not int:
            _invalid_state()
        try:
            authorized = datetime.fromisoformat(_text(row[13]))
            updated = datetime.fromisoformat(_text(row[14]))
        except ValueError:
            _invalid_state()
        result = cls(
            _text(row[0]),
            _text(row[1]),
            row[2],
            _text(row[3]),
            _text(row[4]),
            _text(row[5]),
            _text(row[6]),
            _text(row[7]),
            _text(row[8]),
            _text(row[9]),
            _text(row[10]),
            _text(row[11]),
            row[12],
            authorized,
            updated,
            _text(row[15]),
        )
        result.validate()
        if result.database_values() != row:
            _invalid_state()
        return result

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values: tuple[object, ...] = (
            self.deployment_id,
            self.target,
            self.repository_id,
            self.baseline_sha,
            self.candidate_sha,
            self.backup_slug,
            self.finalization_sha256,
            self.repository_proof_sha256,
            self.backup_proof_sha256,
            self.phase,
            self.reconciliation_state,
            self.block_reason,
            self.attempt_count,
            self.authorized_at.astimezone(UTC).isoformat(),
            self.updated_at.astimezone(UTC).isoformat(),
        )
        return (*values, _digest(values))

    def validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        if (
            _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or _COMMIT.fullmatch(self.baseline_sha) is None
            or _COMMIT.fullmatch(self.candidate_sha) is None
            or self.baseline_sha == self.candidate_sha
            or _SLUG.fullmatch(self.backup_slug) is None
            or any(
                _HASH.fullmatch(value) is None
                for value in (
                    self.finalization_sha256,
                    self.repository_proof_sha256,
                    self.backup_proof_sha256,
                    self.record_sha256,
                )
            )
            or self.phase
            not in {
                "planned",
                "restore_started",
                "restore_acknowledged",
                "uncertain",
                "observing",
                "completed",
                "blocked",
            }
            or self.reconciliation_state
            not in {"none", "not_started", "in_progress", "restored", "ambiguous"}
            or self.block_reason
            not in {
                "none",
                "invalid_authority",
                "backup_invalid",
                "repository_divergence",
                "restore_rejected",
                "ambiguous",
            }
            or type(self.attempt_count) is not int
            or not 0 <= self.attempt_count <= 8
            or not _aware(self.authorized_at)
            or not _aware(self.updated_at)
            or self.updated_at < self.authorized_at
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class RollbackAuthorizationResult:
    status: str
    replayed: bool
    intent: DeploymentRollback


RepositoryReader = Callable[[str, str, int], RollbackRepositoryProof]
BackupReader = Callable[[str, str], RollbackBackupProof]


def authorize_deployment_rollback_once(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    *,
    token: str | None,
    repository_reader: RepositoryReader,
    backup_reader: BackupReader,
    observed_at: datetime | None = None,
) -> RollbackAuthorizationResult:
    """Re-prove exact rollback inputs and persist immutable intent without mutation."""
    try:
        existing = load_deployment_rollback(store, plan)
        if existing is not None:
            return RollbackAuthorizationResult(existing.phase, True, existing)
        prepared, finalization = _failure_authority(store, plan)
        credential = _validate_token(token)
        repository = _read_repository(
            repository_reader,
            prepared.evidence.target,
            credential,
            prepared.evidence.repository_id,
        )
        if (
            repository.repository_id != prepared.evidence.repository_id
            or repository.private is not True
            or repository.main_sha != prepared.evidence.baseline_sha
        ):
            _invalid_proof("repository")
        backup = _read_backup(backup_reader, prepared.evidence.backup_slug, credential)
        if (
            backup.slug != prepared.evidence.backup_slug
            or backup.backup_type != "full"
            or backup.homeassistant_version != prepared.evidence.core_version
            or backup.includes_homeassistant is not True
            or backup.restorable is not True
        ):
            _invalid_proof("backup")
        when = _timestamp(observed_at or datetime.now(UTC))
        if when < finalization.finalized_at:
            _invalid_state()
        requested = DeploymentRollback.create(
            prepared=prepared,
            finalization=finalization,
            repository_proof=repository,
            backup_proof=backup,
            authorized_at=when,
        )
        saved, replayed = _insert_intent(store, plan, requested)
        return RollbackAuthorizationResult(saved.phase, replayed, saved)
    except DeploymentRollbackError:
        raise
    except (
        DeploymentFinalizationError,
        PostDeploymentAssertionObservationError,
        PreparedDeploymentError,
        StateError,
        sqlite3.Error,
        AttributeError,
    ):
        _invalid_state()


def load_deployment_rollback(
    store: StateStore, plan: PostDeploymentAssertionPlan
) -> DeploymentRollback | None:
    try:
        plan._validate()
        deployment_id = plan.automation_target.resource_target.deployment_id
        rows = store._connection.execute(
            "SELECT deployment_id, target, repository_id, baseline_sha, candidate_sha, "
            "backup_slug, finalization_sha256, repository_proof_sha256, "
            "backup_proof_sha256, phase, reconciliation_state, block_reason, "
            "attempt_count, authorized_at, updated_at, record_sha256 "
            "FROM deployment_rollback WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = DeploymentRollback.from_database_row(tuple(rows[0]))
        prepared, finalization = _failure_authority(store, plan)
        if (
            result.target != prepared.evidence.target
            or result.repository_id != prepared.evidence.repository_id
            or result.baseline_sha != prepared.evidence.baseline_sha
            or result.candidate_sha != prepared.evidence.candidate_sha
            or result.backup_slug != prepared.evidence.backup_slug
            or result.finalization_sha256 != finalization.record_sha256
        ):
            _invalid_state()
        return result
    except DeploymentRollbackError:
        raise
    except (
        DeploymentFinalizationError,
        PostDeploymentAssertionObservationError,
        PreparedDeploymentError,
        StateError,
        sqlite3.Error,
        AttributeError,
    ):
        _invalid_state()


def _failure_authority(
    store: StateStore, plan: PostDeploymentAssertionPlan
) -> tuple[PreparedDeployment, DeploymentFinalization]:
    finalization = load_deployment_finalization(store, plan)
    if finalization is None or finalization.outcome != "failure":
        raise DeploymentRollbackError("failure finalization is required")
    prepared = store.prepared_deployment(finalization.deployment_id)
    if prepared is None:
        _invalid_state()
    prepared.validate()
    if (
        finalization.candidate_sha != prepared.evidence.candidate_sha
        or finalization.backup_slug != prepared.evidence.backup_slug
        or store.repository_id(prepared.evidence.target) != prepared.evidence.repository_id
        or not is_candidate_blocked_by_finalization(store, plan, prepared.evidence.candidate_sha)
    ):
        _invalid_state()
    return prepared, finalization


def _insert_intent(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    requested: DeploymentRollback,
) -> tuple[DeploymentRollback, bool]:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _failure_authority(store, plan)
            existing = load_deployment_rollback(store, plan)
            if existing is not None:
                return existing, True
            db.execute(
                "INSERT INTO deployment_rollback VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_deployment_rollback(store, plan)
        if loaded != requested:
            _invalid_state()
        return requested, False
    except DeploymentRollbackError:
        raise
    except (DeploymentFinalizationError, StateError, sqlite3.Error):
        _invalid_state()


def _read_repository(
    reader: RepositoryReader,
    target: str,
    token: str,
    repository_id: int,
) -> RollbackRepositoryProof:
    try:
        result = reader(target, token, repository_id)
        if type(result) is not RollbackRepositoryProof:
            _invalid_proof("repository")
        result.validate()
        return result
    except DeploymentRollbackError:
        raise
    except Exception:
        raise DeploymentRollbackError("repository proof is unavailable") from None


def _read_backup(reader: BackupReader, slug: str, token: str) -> RollbackBackupProof:
    try:
        result = reader(slug, token)
        if type(result) is not RollbackBackupProof:
            _invalid_proof("backup")
        result.validate()
        return result
    except DeploymentRollbackError:
        raise
    except Exception:
        raise DeploymentRollbackError("backup proof is unavailable") from None


def _validate_token(token: str | None) -> str:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise DeploymentRollbackError("rollback proof credential is unavailable")
    return token


def _repository_proof_digest(proof: RollbackRepositoryProof) -> str:
    proof.validate()
    return _digest((proof.repository_id, proof.private, proof.main_sha))


def _backup_proof_digest(proof: RollbackBackupProof) -> str:
    proof.validate()
    return _digest(
        (
            proof.slug,
            proof.backup_type,
            proof.homeassistant_version,
            proof.includes_homeassistant,
            proof.restorable,
        )
    )


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _timestamp(value: datetime) -> datetime:
    if not _aware(value):
        _invalid_state()
    return value.astimezone(UTC)


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid_state()
    return value


def _invalid_proof(kind: str) -> NoReturn:
    raise DeploymentRollbackError(f"{kind} proof is invalid") from None


def _invalid_state() -> NoReturn:
    raise DeploymentRollbackError("deployment rollback state is invalid") from None
