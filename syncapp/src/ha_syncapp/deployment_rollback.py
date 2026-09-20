"""Immutable authorization boundary for crash-safe deployment rollback."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, NoReturn

from .core_health_observation import (
    CoreHealthError,
    CoreHealthTransport,
    probe_core_api_health,
)
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
from .supervisor_health_observation import (
    SupervisorHealthError,
    SupervisorHealthTransport,
    probe_supervisor_health,
)

_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CORE = re.compile(r"^20[0-9]{2}\.(?:[1-9]|1[0-2])\.(?:0|[1-9][0-9]*)$")
_TOKEN = re.compile(r"^[!-~]{1,512}$")
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SUPERVISOR_ROOT: Final = "http://supervisor"
_RESTORE_BODY: Final = json.dumps({"background": True}, separators=(",", ":")).encode("ascii")
_DEFAULT_TIMEOUT_SECONDS: Final = 60.0
_DEFAULT_MAX_RESPONSE_BYTES: Final = 64 * 1024


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
class SupervisorRestoreResponse:
    status: int
    content_type: str
    body: bytes


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
    restore_job_id: str | None
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
            None,
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
            None,
            when,
            when,
            _digest(values),
        )
        result.validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> DeploymentRollback:
        if len(row) != 17 or type(row[2]) is not int or type(row[12]) is not int:
            _invalid_state()
        try:
            authorized = datetime.fromisoformat(_text(row[14]))
            updated = datetime.fromisoformat(_text(row[15]))
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
            None if row[13] is None else _text(row[13]),
            authorized,
            updated,
            _text(row[16]),
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
            self.restore_job_id,
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
            or (self.restore_job_id is not None and _JOB_ID.fullmatch(self.restore_job_id) is None)
            or (self.phase == "restore_acknowledged" and self.restore_job_id is None)
            or (
                self.phase in {"planned", "restore_started", "uncertain", "blocked"}
                and self.restore_job_id is not None
            )
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
SupervisorRestoreTransport = Callable[
    [str, str, Mapping[str, str], bytes, float, int], SupervisorRestoreResponse
]


@dataclass(frozen=True, slots=True)
class RollbackRestoreResult:
    status: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class _SupervisorRestoreJob:
    job_id: str
    name: str
    reference: str
    done: bool | None
    errors_present: bool
    created_at: datetime


def reconcile_deployment_restore_once(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    *,
    supervisor_token: str | None,
    transport: SupervisorRestoreTransport | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    observed_at: datetime | None = None,
) -> RollbackRestoreResult:
    """Read authoritative Supervisor job evidence without repeating a restore."""
    current = load_deployment_rollback(store, plan)
    if current is None:
        raise DeploymentRollbackError("rollback intent is required")
    if current.phase in {"observing", "completed"}:
        return RollbackRestoreResult("restored", True)
    if current.phase == "blocked":
        status = "ambiguous" if current.block_reason == "ambiguous" else "blocked"
        return RollbackRestoreResult(status, True)
    if current.phase not in {"restore_acknowledged", "restore_started", "uncertain"}:
        raise DeploymentRollbackError("rollback restore is not reconcilable")

    credential = _validate_token(supervisor_token, "SUPERVISOR_TOKEN")
    _validate_limits(timeout_seconds, max_response_bytes)
    sender = transport or _default_reconciliation_transport
    job_id = current.restore_job_id
    url = (
        f"{_SUPERVISOR_ROOT}/jobs/{job_id}"
        if job_id is not None
        else f"{_SUPERVISOR_ROOT}/jobs/info"
    )
    try:
        response = sender(
            "GET",
            url,
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {credential}",
            },
            b"",
            timeout_seconds,
            max_response_bytes,
        )
    except Exception:
        raise DeploymentRollbackError(
            "rollback reconciliation is temporarily unavailable"
        ) from None

    job = _reconciled_restore_job(response, current, max_response_bytes)
    when = _timestamp(observed_at or datetime.now(UTC))
    if when < current.updated_at:
        _invalid_state()
    if job is None or job.done is False or job.errors_present:
        _transition(
            store,
            plan,
            current,
            phase="blocked",
            reconciliation_state="ambiguous",
            block_reason="ambiguous",
            attempt_count=current.attempt_count,
            restore_job_id=None,
            when=when,
        )
        return RollbackRestoreResult("ambiguous", False)
    if job.done is None:
        if current.restore_job_id == job.job_id:
            return RollbackRestoreResult("in_progress", False)
        _transition(
            store,
            plan,
            current,
            phase="restore_acknowledged",
            reconciliation_state="in_progress",
            block_reason="none",
            attempt_count=current.attempt_count,
            restore_job_id=job.job_id,
            when=when,
        )
        return RollbackRestoreResult("in_progress", False)
    _transition(
        store,
        plan,
        current,
        phase="observing",
        reconciliation_state="restored",
        block_reason="none",
        attempt_count=current.attempt_count,
        restore_job_id=job.job_id,
        when=when,
    )
    return RollbackRestoreResult("restored", False)


def complete_deployment_rollback_once(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    *,
    github_token: str | None,
    supervisor_token: str | None,
    repository_reader: RepositoryReader,
    core_transport: CoreHealthTransport | None = None,
    supervisor_transport: SupervisorHealthTransport | None = None,
    timeout_seconds: float = 10.0,
    max_response_bytes: int = 16 * 1024,
    observed_at: datetime | None = None,
) -> RollbackRestoreResult:
    """Complete rollback only after exact baseline and bounded health proofs."""
    current = load_deployment_rollback(store, plan)
    if current is None:
        raise DeploymentRollbackError("rollback intent is required")
    if current.phase == "completed":
        return RollbackRestoreResult("completed", True)
    if current.phase == "blocked":
        return RollbackRestoreResult("blocked", True)
    if (
        current.phase != "observing"
        or current.reconciliation_state != "restored"
        or current.restore_job_id is None
    ):
        raise DeploymentRollbackError("rollback restore is not ready for health proof")

    repository_credential = _validate_token(github_token, "SYNCAPP_GITHUB_TOKEN")
    supervisor_credential = _validate_token(supervisor_token, "SUPERVISOR_TOKEN")
    repository = _read_repository(
        repository_reader,
        current.target,
        repository_credential,
        current.repository_id,
    )
    if (
        repository.repository_id != current.repository_id
        or repository.private is not True
        or repository.main_sha != current.baseline_sha
        or _repository_proof_digest(repository) != current.repository_proof_sha256
    ):
        when = _timestamp(observed_at or datetime.now(UTC))
        if when < current.updated_at:
            _invalid_state()
        _transition(
            store,
            plan,
            current,
            phase="blocked",
            reconciliation_state="restored",
            block_reason="repository_divergence",
            attempt_count=current.attempt_count,
            restore_job_id=None,
            when=when,
        )
        return RollbackRestoreResult("blocked", False)
    try:
        probe_core_api_health(
            token=supervisor_credential,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            transport=core_transport,
        )
        probe_supervisor_health(
            token=supervisor_credential,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            transport=supervisor_transport,
        )
    except (CoreHealthError, SupervisorHealthError):
        raise DeploymentRollbackError("rollback post-restore health is unavailable") from None

    when = _timestamp(observed_at or datetime.now(UTC))
    if when < current.updated_at:
        _invalid_state()
    _transition(
        store,
        plan,
        current,
        phase="completed",
        reconciliation_state="restored",
        block_reason="none",
        attempt_count=current.attempt_count,
        restore_job_id=current.restore_job_id,
        when=when,
    )
    return RollbackRestoreResult("completed", False)


def request_deployment_restore_once(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    *,
    github_token: str | None,
    supervisor_token: str | None,
    repository_reader: RepositoryReader,
    backup_reader: BackupReader,
    transport: SupervisorRestoreTransport | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    requested_at: datetime | None = None,
) -> RollbackRestoreResult:
    """Journal before one exact restore request and never blindly replay it."""
    current = load_deployment_rollback(store, plan)
    if current is None:
        raise DeploymentRollbackError("rollback intent is required")
    if current.phase == "restore_acknowledged":
        return RollbackRestoreResult("restore_acknowledged", True)
    if current.phase in {"restore_started", "uncertain"}:
        return RollbackRestoreResult("reconciliation_required", True)
    if current.phase == "blocked":
        return RollbackRestoreResult("blocked", True)
    if current.phase != "planned":
        raise DeploymentRollbackError("rollback restore is not requestable")

    repository_credential = _validate_token(github_token, "SYNCAPP_GITHUB_TOKEN")
    supervisor_credential = _validate_token(supervisor_token, "SUPERVISOR_TOKEN")
    _validate_limits(timeout_seconds, max_response_bytes)
    _reprove_external_inputs(
        current,
        repository_credential,
        supervisor_credential,
        repository_reader,
        backup_reader,
    )
    when = _timestamp(requested_at or datetime.now(UTC))
    started = _transition(
        store,
        plan,
        current,
        phase="restore_started",
        reconciliation_state="none",
        block_reason="none",
        attempt_count=current.attempt_count + 1,
        restore_job_id=None,
        when=when,
    )
    sender = transport or _default_restore_transport
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {supervisor_credential}",
        "Content-Type": "application/json",
    }
    url = f"{_SUPERVISOR_ROOT}/backups/{started.backup_slug}/restore/full"
    try:
        response = sender(
            "POST",
            url,
            headers,
            _RESTORE_BODY,
            timeout_seconds,
            max_response_bytes,
        )
    except Exception:
        _transition(
            store,
            plan,
            started,
            phase="uncertain",
            reconciliation_state="ambiguous",
            block_reason="none",
            attempt_count=started.attempt_count,
            restore_job_id=None,
            when=when,
        )
        raise DeploymentRollbackError("rollback restore outcome is uncertain") from None
    if _definitely_rejected(response):
        _transition(
            store,
            plan,
            started,
            phase="blocked",
            reconciliation_state="none",
            block_reason="restore_rejected",
            attempt_count=started.attempt_count,
            restore_job_id=None,
            when=when,
        )
        return RollbackRestoreResult("blocked", False)
    try:
        job_id = _restore_job_id(response, max_response_bytes)
    except DeploymentRollbackError:
        _transition(
            store,
            plan,
            started,
            phase="uncertain",
            reconciliation_state="ambiguous",
            block_reason="none",
            attempt_count=started.attempt_count,
            restore_job_id=None,
            when=when,
        )
        raise DeploymentRollbackError("rollback restore outcome is uncertain") from None
    _transition(
        store,
        plan,
        started,
        phase="restore_acknowledged",
        reconciliation_state="in_progress",
        block_reason="none",
        attempt_count=started.attempt_count,
        restore_job_id=job_id,
        when=when,
    )
    return RollbackRestoreResult("restore_acknowledged", False)


def authorize_deployment_rollback_once(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    *,
    github_token: str | None,
    supervisor_token: str | None,
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
        repository_credential = _validate_token(github_token, "SYNCAPP_GITHUB_TOKEN")
        supervisor_credential = _validate_token(supervisor_token, "SUPERVISOR_TOKEN")
        repository = _read_repository(
            repository_reader,
            prepared.evidence.target,
            repository_credential,
            prepared.evidence.repository_id,
        )
        if (
            repository.repository_id != prepared.evidence.repository_id
            or repository.private is not True
            or repository.main_sha != prepared.evidence.baseline_sha
        ):
            _invalid_proof("repository")
        backup = _read_backup(backup_reader, prepared.evidence.backup_slug, supervisor_credential)
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
            "attempt_count, restore_job_id, authorized_at, updated_at, record_sha256 "
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
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def _reprove_external_inputs(
    intent: DeploymentRollback,
    github_token: str,
    supervisor_token: str,
    repository_reader: RepositoryReader,
    backup_reader: BackupReader,
) -> None:
    repository = _read_repository(
        repository_reader,
        intent.target,
        github_token,
        intent.repository_id,
    )
    if (
        repository.repository_id != intent.repository_id
        or repository.private is not True
        or repository.main_sha != intent.baseline_sha
        or _repository_proof_digest(repository) != intent.repository_proof_sha256
    ):
        _invalid_proof("repository")
    backup = _read_backup(backup_reader, intent.backup_slug, supervisor_token)
    if (
        backup.slug != intent.backup_slug
        or backup.backup_type != "full"
        or backup.includes_homeassistant is not True
        or backup.restorable is not True
        or _backup_proof_digest(backup) != intent.backup_proof_sha256
    ):
        _invalid_proof("backup")


def _transition(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    current: DeploymentRollback,
    *,
    phase: str,
    reconciliation_state: str,
    block_reason: str,
    attempt_count: int,
    restore_job_id: str | None,
    when: datetime,
) -> DeploymentRollback:
    updated = _timestamp(when)
    values: tuple[object, ...] = (
        current.deployment_id,
        current.target,
        current.repository_id,
        current.baseline_sha,
        current.candidate_sha,
        current.backup_slug,
        current.finalization_sha256,
        current.repository_proof_sha256,
        current.backup_proof_sha256,
        phase,
        reconciliation_state,
        block_reason,
        attempt_count,
        restore_job_id,
        current.authorized_at.astimezone(UTC).isoformat(),
        updated.isoformat(),
    )
    replacement = DeploymentRollback(
        current.deployment_id,
        current.target,
        current.repository_id,
        current.baseline_sha,
        current.candidate_sha,
        current.backup_slug,
        current.finalization_sha256,
        current.repository_proof_sha256,
        current.backup_proof_sha256,
        phase,
        reconciliation_state,
        block_reason,
        attempt_count,
        restore_job_id,
        current.authorized_at,
        updated,
        _digest(values),
    )
    replacement.validate()
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            loaded = load_deployment_rollback(store, plan)
            if loaded != current:
                _invalid_state()
            changed = db.execute(
                "UPDATE deployment_rollback SET phase = ?, reconciliation_state = ?, "
                "block_reason = ?, attempt_count = ?, restore_job_id = ?, updated_at = ?, "
                "record_sha256 = ? WHERE deployment_id = ? AND record_sha256 = ?",
                (
                    replacement.phase,
                    replacement.reconciliation_state,
                    replacement.block_reason,
                    replacement.attempt_count,
                    replacement.restore_job_id,
                    replacement.updated_at.isoformat(),
                    replacement.record_sha256,
                    replacement.deployment_id,
                    current.record_sha256,
                ),
            ).rowcount
            if changed != 1:
                _invalid_state()
        loaded = load_deployment_rollback(store, plan)
        if loaded != replacement:
            _invalid_state()
        return replacement
    except DeploymentRollbackError:
        raise
    except (StateError, sqlite3.Error):
        _invalid_state()


def _definitely_rejected(response: SupervisorRestoreResponse) -> bool:
    if type(response) is not SupervisorRestoreResponse:
        return False
    return response.status in {400, 401, 403, 404, 409, 422}


def _restore_job_id(response: SupervisorRestoreResponse, max_response_bytes: int) -> str:
    if (
        type(response) is not SupervisorRestoreResponse
        or response.status != 200
        or _media_type(response.content_type) != "application/json"
        or len(response.body) > max_response_bytes
    ):
        raise DeploymentRollbackError("rollback restore response is invalid")
    try:
        payload = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise DeploymentRollbackError("rollback restore response is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {"result", "data"}:
        raise DeploymentRollbackError("rollback restore response is invalid")
    data = payload.get("data")
    if payload.get("result") != "ok" or not isinstance(data, dict):
        raise DeploymentRollbackError("rollback restore response is invalid")
    if set(data) != {"job_id"}:
        raise DeploymentRollbackError("rollback restore response is invalid")
    job_id = data.get("job_id")
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise DeploymentRollbackError("rollback restore response is invalid")
    return job_id


def _reconciled_restore_job(
    response: SupervisorRestoreResponse,
    intent: DeploymentRollback,
    max_response_bytes: int,
) -> _SupervisorRestoreJob | None:
    if (
        type(response) is not SupervisorRestoreResponse
        or response.status != 200
        or _media_type(response.content_type) != "application/json"
        or len(response.body) > max_response_bytes
    ):
        return None
    try:
        payload = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"result", "data"}:
        return None
    data = payload.get("data")
    if payload.get("result") != "ok" or not isinstance(data, dict):
        return None
    if intent.restore_job_id is not None:
        job = _parse_restore_job(data, intent)
        if job is None or job.job_id != intent.restore_job_id:
            return None
        return job
    if set(data) != {"ignore_conditions", "jobs"} or not isinstance(data.get("jobs"), list):
        return None
    matching: list[_SupervisorRestoreJob | None] = []
    for candidate in data["jobs"]:
        if not isinstance(candidate, dict):
            continue
        if (
            candidate.get("name") == "backup_manager_full_restore"
            and candidate.get("reference") == intent.backup_slug
        ):
            matching.append(_parse_restore_job(candidate, intent))
    if len(matching) != 1:
        return None
    job = matching[0]
    if job is None or job.created_at < intent.updated_at:
        return None
    return job


def _parse_restore_job(
    value: dict[object, object], intent: DeploymentRollback
) -> _SupervisorRestoreJob | None:
    required = {
        "name",
        "reference",
        "uuid",
        "progress",
        "stage",
        "done",
        "errors",
        "created",
        "extra",
        "child_jobs",
    }
    if set(value) != required:
        return None
    job_id = value.get("uuid")
    name = value.get("name")
    reference = value.get("reference")
    done = value.get("done")
    errors = value.get("errors")
    created = value.get("created")
    if (
        not isinstance(job_id, str)
        or _JOB_ID.fullmatch(job_id) is None
        or name != "backup_manager_full_restore"
        or reference != intent.backup_slug
        or (done is not None and type(done) is not bool)
        or not isinstance(errors, list)
        or not isinstance(created, str)
        or not isinstance(value.get("child_jobs"), list)
    ):
        return None
    try:
        created_at = datetime.fromisoformat(created)
    except ValueError:
        return None
    if not _aware(created_at) or created_at < intent.authorized_at:
        return None
    return _SupervisorRestoreJob(
        job_id,
        name,
        reference,
        done,
        bool(errors),
        created_at.astimezone(UTC),
    )


def _default_restore_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
) -> SupervisorRestoreResponse:
    prefix = f"{_SUPERVISOR_ROOT}/backups/"
    if method != "POST" or not url.startswith(prefix) or body != _RESTORE_BODY:
        raise DeploymentRollbackError("rollback restore request boundary is invalid")
    suffix = url.removeprefix(_SUPERVISOR_ROOT)
    parts = suffix.split("/")
    if (
        len(parts) != 5
        or parts[1] != "backups"
        or _SLUG.fullmatch(parts[2]) is None
        or parts[3:] != ["restore", "full"]
    ):
        raise DeploymentRollbackError("rollback restore request boundary is invalid")
    connection = http.client.HTTPConnection("supervisor", 80, timeout=timeout_seconds)
    try:
        connection.request("POST", suffix, body=body, headers=dict(headers))
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "") or ""
        content_length = response.getheader("Content-Length")
        if content_length is not None and int(content_length) > max_response_bytes:
            raise DeploymentRollbackError("rollback restore response exceeds limit")
        response_body = response.read(max_response_bytes + 1)
        status = int(response.status)
    except DeploymentRollbackError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        raise DeploymentRollbackError("rollback restore request failed") from None
    finally:
        connection.close()
    return SupervisorRestoreResponse(status, content_type, response_body)


def _default_reconciliation_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
    max_response_bytes: int,
) -> SupervisorRestoreResponse:
    prefix = f"{_SUPERVISOR_ROOT}/jobs/"
    if method != "GET" or body or not url.startswith(prefix):
        raise DeploymentRollbackError("rollback reconciliation boundary is invalid")
    suffix = url.removeprefix(_SUPERVISOR_ROOT)
    parts = suffix.split("/")
    if (
        len(parts) != 3
        or parts[1] != "jobs"
        or (parts[2] != "info" and _JOB_ID.fullmatch(parts[2]) is None)
    ):
        raise DeploymentRollbackError("rollback reconciliation boundary is invalid")
    connection = http.client.HTTPConnection("supervisor", 80, timeout=timeout_seconds)
    try:
        connection.request("GET", suffix, headers=dict(headers))
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "") or ""
        content_length = response.getheader("Content-Length")
        if content_length is not None and int(content_length) > max_response_bytes:
            raise DeploymentRollbackError("rollback reconciliation response exceeds limit")
        response_body = response.read(max_response_bytes + 1)
        status = int(response.status)
    except DeploymentRollbackError:
        raise
    except (http.client.HTTPException, TimeoutError, OSError, ValueError):
        raise DeploymentRollbackError("rollback reconciliation request failed") from None
    finally:
        connection.close()
    return SupervisorRestoreResponse(status, content_type, response_body)


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


def _validate_token(token: str | None, environment_name: str) -> str:
    candidate = token if token is not None else os.environ.get(environment_name)
    if not isinstance(candidate, str) or _TOKEN.fullmatch(candidate) is None:
        raise DeploymentRollbackError("rollback proof credential is unavailable")
    return candidate


def _validate_limits(timeout_seconds: float, max_response_bytes: int) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or timeout_seconds <= 0
        or timeout_seconds > 60
        or type(max_response_bytes) is not int
        or not 0 < max_response_bytes <= 1024 * 1024
    ):
        raise DeploymentRollbackError("rollback restore transport limits are invalid")


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


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
