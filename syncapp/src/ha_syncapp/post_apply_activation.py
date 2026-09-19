"""Durable, fail-closed authorization for post-Apply Core activation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .apply_authorization import ApplyAuthorization
from .candidate_stage import CandidateStage, verify_candidate_stage
from .live_apply_intent import LiveApplyIntent, derive_live_apply_intent
from .live_apply_intent_store import PersistedLiveApplyIntent, load_live_apply_intent
from .live_apply_plan import LiveApplyPlan
from .live_apply_preconditions import LiveApplyPreconditionEvidence
from .live_apply_progress_store import (
    discover_live_apply_progress,
    discover_live_apply_recovery,
)
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .stage_prewrite_reproof import StagePrewriteEvidence
from .state import StateError, StateStore

_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class PostApplyActivationError(RuntimeError):
    """Post-Apply activation could not be authorized safely."""


@dataclass(frozen=True, slots=True, init=False)
class PostApplyActivationAuthorization:
    """Durable evidence that exactly one completed Apply plan may activate Core."""

    deployment_id: str
    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    backup_slug: str
    intent_record_sha256: str
    operations_sha256: str
    operation_count: int
    action: str
    authorized_at: datetime
    record_sha256: str

    @classmethod
    def create(
        cls,
        intent: PersistedLiveApplyIntent,
        operation_count: int,
        authorized_at: datetime,
    ) -> PostApplyActivationAuthorization:
        when = _timestamp(authorized_at)
        values: tuple[object, ...] = (
            intent.deployment_id,
            intent.target,
            intent.repository_id,
            intent.baseline_sha,
            intent.candidate_sha,
            intent.stage_manifest_sha256,
            intent.backup_slug,
            intent.record_sha256,
            intent.operations_sha256,
            operation_count,
            "restart_core",
            when.isoformat(),
        )
        return _construct_authorization((*values[:-1], when, _record_digest(values)))

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> PostApplyActivationAuthorization:
        if len(row) != 13:
            _invalid_record()
        repository_id = row[2]
        operation_count = row[9]
        if type(repository_id) is not int or type(operation_count) is not int:
            _invalid_record()
        authorized_at_text = _text(row[11])
        try:
            authorized_at = datetime.fromisoformat(authorized_at_text)
        except ValueError:
            _invalid_record()
        record = _construct_authorization(
            (
                _text(row[0]),
                _text(row[1]),
                repository_id,
                _text(row[3]),
                _text(row[4]),
                _text(row[5]),
                _text(row[6]),
                _text(row[7]),
                _text(row[8]),
                operation_count,
                _text(row[10]),
                authorized_at,
                _text(row[12]),
            )
        )
        record._validate()
        if record.database_values() != row:
            _invalid_record()
        return record

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        values: tuple[object, ...] = (
            *astuple(self)[:11],
            self.authorized_at.astimezone(UTC).isoformat(),
        )
        return (*values, _record_digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_record()
        if (
            _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or not 0 < self.repository_id <= 2**63 - 1
            or _COMMIT.fullmatch(self.baseline_sha) is None
            or _COMMIT.fullmatch(self.candidate_sha) is None
            or len(self.baseline_sha) != len(self.candidate_sha)
            or self.baseline_sha == self.candidate_sha
            or _HASH.fullmatch(self.stage_manifest_sha256) is None
            or _SLUG.fullmatch(self.backup_slug) is None
            or _HASH.fullmatch(self.intent_record_sha256) is None
            or _HASH.fullmatch(self.operations_sha256) is None
            or type(self.operation_count) is not int
            or self.operation_count <= 0
            or self.action != "restart_core"
            or not isinstance(self.authorized_at, datetime)
            or self.authorized_at.tzinfo is None
            or self.authorized_at.utcoffset() is None
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_record()


@dataclass(frozen=True, slots=True)
class PostApplyActivationResult:
    """Sanitized outcome of the side-effect-free activation gate."""

    action: str
    authorization: PostApplyActivationAuthorization | None
    replayed: bool


def authorize_post_apply_activation(
    store: StateStore,
    apply_authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
    *,
    authorized_at: datetime | None = None,
) -> PostApplyActivationResult:
    """Persist exact activation authority only after a fully verified non-empty Apply."""
    intent = _prove_complete_chain(
        store, apply_authorization, stage_evidence, stage, plan, preconditions
    )
    if not plan.operations:
        return PostApplyActivationResult("no_activation_required", None, replayed=True)
    when = _timestamp(authorized_at)
    requested = PostApplyActivationAuthorization.create(intent, len(plan.operations), when)
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            current_intent = _prove_complete_chain(
                store, apply_authorization, stage_evidence, stage, plan, preconditions
            )
            if current_intent.record_sha256 != intent.record_sha256:
                _reject("post-Apply activation evidence changed unexpectedly")
            row = _select_row(db, intent.deployment_id)
            if row is not None:
                existing = _parse_and_revalidate(store, row)
                if _identity(existing) != _identity(requested):
                    _reject("post-Apply activation authorization cannot be rebound")
                return PostApplyActivationResult("restart_core", existing, replayed=True)
            db.execute(
                "INSERT INTO post_apply_activation_authorization (deployment_id, target, "
                "repository_id, baseline_sha, candidate_sha, stage_manifest_sha256, "
                "backup_slug, intent_record_sha256, operations_sha256, operation_count, "
                "action, authorized_at, record_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_post_apply_activation_authorization(store, intent.deployment_id)
        if loaded is None or loaded != requested:
            _reject("post-Apply activation authorization was not persisted")
        return PostApplyActivationResult("restart_core", loaded, replayed=False)
    except PostApplyActivationError:
        raise
    except (StateError, sqlite3.Error):
        _reject("post-Apply activation state is invalid")


def load_post_apply_activation_authorization(
    store: StateStore, deployment_id: str
) -> PostApplyActivationAuthorization | None:
    """Load authorization while rechecking its durable Apply-intent binding."""
    if type(store) is not StateStore:
        _reject("post-Apply activation state is invalid")
    try:
        validate_deployment_id(deployment_id)
        row = _select_row(store._connection, deployment_id)
        if row is None:
            return None
        return _parse_and_revalidate(store, row)
    except (PreparedDeploymentError, StateError, sqlite3.Error):
        _reject("post-Apply activation state is invalid")


def _prove_complete_chain(
    store: StateStore,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    stage: CandidateStage,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
) -> PersistedLiveApplyIntent:
    if type(store) is not StateStore or type(stage) is not CandidateStage:
        _reject("post-Apply activation evidence is invalid")
    try:
        verify_candidate_stage(stage)
        derived: LiveApplyIntent = derive_live_apply_intent(
            authorization, stage_evidence, plan, preconditions
        )
        persisted = load_live_apply_intent(store, derived.deployment_id)
        recovery = discover_live_apply_recovery(store, plan)
        progress = discover_live_apply_progress(store, derived.deployment_id)
    except Exception:
        _reject("post-Apply activation evidence is invalid")
    if persisted is None:
        _reject("post-Apply activation evidence is invalid")
    if (
        stage.target,
        stage.repository_id,
        stage.branch,
        stage.commit_sha,
        stage.manifest_sha256,
    ) != (
        derived.target,
        derived.repository_id,
        "candidate",
        derived.candidate_sha,
        derived.stage_manifest_sha256,
    ):
        _reject("post-Apply activation evidence is invalid")
    if _intent_identity(persisted) != _derived_identity(derived):
        _reject("post-Apply activation evidence is invalid")
    if recovery.action != "complete" or recovery.operation_index is not None:
        _reject("post-Apply activation is not complete")
    if len(progress) != len(plan.operations) or any(
        record.operation_index != index or record.phase != "mutation_verified"
        for index, record in enumerate(progress)
    ):
        _reject("post-Apply activation is not complete")
    return persisted


def _select_row(db: sqlite3.Connection, deployment_id: str) -> tuple[object, ...] | None:
    rows = db.execute(
        "SELECT deployment_id, target, repository_id, baseline_sha, candidate_sha, "
        "stage_manifest_sha256, backup_slug, intent_record_sha256, operations_sha256, "
        "operation_count, action, authorized_at, record_sha256 "
        "FROM post_apply_activation_authorization WHERE deployment_id = ?",
        (deployment_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        _invalid_record()
    return tuple(rows[0])


def _parse_and_revalidate(
    store: StateStore, row: tuple[object, ...]
) -> PostApplyActivationAuthorization:
    record = PostApplyActivationAuthorization.from_database_row(row)
    intent = load_live_apply_intent(store, record.deployment_id)
    if (
        intent is None
        or record.intent_record_sha256 != intent.record_sha256
        or _authorization_intent_identity(record) != _intent_identity(intent)
    ):
        _invalid_record()
    return record


def _intent_identity(intent: PersistedLiveApplyIntent) -> tuple[object, ...]:
    return (
        intent.deployment_id,
        intent.target,
        intent.repository_id,
        intent.baseline_sha,
        intent.candidate_sha,
        intent.stage_manifest_sha256,
        intent.backup_slug,
        intent.operations_sha256,
    )


def _derived_identity(intent: LiveApplyIntent) -> tuple[object, ...]:
    return (
        intent.deployment_id,
        intent.target,
        intent.repository_id,
        intent.baseline_sha,
        intent.candidate_sha,
        intent.stage_manifest_sha256,
        intent.backup_slug,
        intent.operations_sha256,
    )


def _authorization_intent_identity(
    record: PostApplyActivationAuthorization,
) -> tuple[object, ...]:
    return (
        record.deployment_id,
        record.target,
        record.repository_id,
        record.baseline_sha,
        record.candidate_sha,
        record.stage_manifest_sha256,
        record.backup_slug,
        record.operations_sha256,
    )


def _identity(record: PostApplyActivationAuthorization) -> tuple[object, ...]:
    return astuple(record)[:11]


def _construct_authorization(
    values: tuple[object, ...],
) -> PostApplyActivationAuthorization:
    if len(values) != 13:
        _invalid_record()
    result = object.__new__(PostApplyActivationAuthorization)
    for name, value in zip(PostApplyActivationAuthorization.__slots__, values, strict=True):
        object.__setattr__(result, name, value)
    result._validate()
    return result


def _timestamp(value: datetime | None) -> datetime:
    when = datetime.now(UTC) if value is None else value
    if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
        _reject("post-Apply activation timestamp is invalid")
    return when.astimezone(UTC)


def _record_digest(values: tuple[object, ...]) -> str:
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid_record()
    return value


def _invalid_record() -> NoReturn:
    _reject("post-Apply activation state is invalid")


def _reject(message: str) -> NoReturn:
    raise PostApplyActivationError(message) from None
