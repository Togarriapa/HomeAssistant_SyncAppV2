"""Guarded, idempotent publication of finalized successful deployments.

This module deliberately separates durable authorization/evidence from the GitHub
transport.  The transport receives an immutable intent plus the remote state it
was derived from; it must implement compare-and-swap semantics and must never
force refs or merge divergent configuration states.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, NoReturn

from .deployment_finalization import (
    DeploymentFinalizationError,
    candidate_finalization_authority,
    load_deployment_finalization,
)
from .post_deployment_assertion_observation import PostDeploymentAssertionPlan
from .prepared_deployment import PreparedDeploymentError
from .state import StateError, StateStore

_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PHASES = {"planned", "completed", "blocked"}


class DeploymentPromotionError(RuntimeError):
    """Promotion is unavailable or deterministically unsafe."""


@dataclass(frozen=True, slots=True)
class PromotionRemoteState:
    """Minimal content-free Repo B state needed for guarded publication."""

    candidate_sha: str
    main_sha: str
    tag_sha: str | None

    def validate(self) -> None:
        if (
            _COMMIT.fullmatch(self.candidate_sha) is None
            or _COMMIT.fullmatch(self.main_sha) is None
            or (self.tag_sha is not None and _COMMIT.fullmatch(self.tag_sha) is None)
        ):
            _invalid()


@dataclass(frozen=True, slots=True)
class DeploymentPromotion:
    deployment_id: str
    target: str
    repository_id: int
    candidate_sha: str
    prior_main_sha: str
    finalization_sha256: str
    backup_slug: str
    tag: str
    phase: str
    recorded_at: datetime
    completed_at: datetime | None
    record_sha256: str

    def values_without_digest(self) -> tuple[object, ...]:
        return (
            self.deployment_id,
            self.target,
            self.repository_id,
            self.candidate_sha,
            self.prior_main_sha,
            self.finalization_sha256,
            self.backup_slug,
            self.tag,
            self.phase,
            self.recorded_at.astimezone(UTC).isoformat(),
            None if self.completed_at is None else self.completed_at.astimezone(UTC).isoformat(),
        )

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values = self.values_without_digest()
        return (*values, _digest(values))

    def validate(self) -> None:
        if (
            not self.deployment_id
            or not self.target
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or _COMMIT.fullmatch(self.candidate_sha) is None
            or _COMMIT.fullmatch(self.prior_main_sha) is None
            or _HASH.fullmatch(self.finalization_sha256) is None
            or not self.backup_slug
            or not self.tag.startswith("syncapp-known-good-")
            or self.phase not in _PHASES
            or self.recorded_at.tzinfo is None
            or (self.completed_at is not None and self.completed_at.tzinfo is None)
            or (self.phase == "completed") != (self.completed_at is not None)
            or _HASH.fullmatch(self.record_sha256) is None
            or self.record_sha256 != _digest(self.values_without_digest())
        ):
            _invalid()


@dataclass(frozen=True, slots=True)
class DeploymentPromotionResult:
    status: str
    replayed: bool
    tag: str
    promoted_sha: str


RemoteReader = Callable[[str, str, int, str], PromotionRemoteState]
Publisher = Callable[[DeploymentPromotion, PromotionRemoteState, str], None]


def promote_finalized_deployment_once(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    *,
    token: str | None,
    remote_reader: RemoteReader,
    publisher: Publisher,
    observed_at: datetime | None = None,
) -> DeploymentPromotionResult:
    """Publish one exact finalized success or reconcile a prior publication.

    Deterministic authority/ref conflicts are blocked.  Transport exceptions are
    intentionally reported as retryable/unavailable without leaking their text.
    """
    try:
        existing = load_deployment_promotion(store, plan)
        if existing is not None and existing.phase == "completed":
            return _result(existing, True)

        prepared, finalization = _authority(store, plan)
        when = _timestamp(observed_at or datetime.now(UTC))
        intended = existing or _new_intent(prepared, finalization.record_sha256, when)
        if existing is None:
            _persist(store, intended)

        if token is None or not isinstance(token, str) or not token:
            _invalid()

        try:
            remote = remote_reader(
                intended.target, token, intended.repository_id, intended.tag
            )
        except Exception:
            raise DeploymentPromotionError("Promotion transport is unavailable") from None
        remote.validate()

        # Crash reconciliation: both refs already equal the exact authorized SHA.
        if remote.main_sha == intended.candidate_sha and remote.tag_sha == intended.candidate_sha:
            return _complete(store, intended, when, replayed=True)

        # Safe partial publication is allowed only when each ref is either its exact
        # pre-state or its exact authorized post-state. Candidate must never move.
        if (
            remote.candidate_sha != intended.candidate_sha
            or remote.main_sha not in {intended.prior_main_sha, intended.candidate_sha}
            or remote.tag_sha not in {None, intended.candidate_sha}
        ):
            _block(store, intended)
            raise DeploymentPromotionError("Promotion is blocked by remote divergence")

        try:
            publisher(intended, remote, token)
            verified = remote_reader(
                intended.target, token, intended.repository_id, intended.tag
            )
        except Exception:
            raise DeploymentPromotionError("Promotion transport is unavailable") from None
        verified.validate()
        if (
            verified.candidate_sha != intended.candidate_sha
            or verified.main_sha != intended.candidate_sha
            or verified.tag_sha != intended.candidate_sha
        ):
            raise DeploymentPromotionError("Promotion transport is unavailable")
        return _complete(store, intended, when, replayed=False)
    except DeploymentPromotionError:
        raise
    except (DeploymentFinalizationError, PreparedDeploymentError, StateError, sqlite3.Error, AttributeError):
        _invalid()


def load_deployment_promotion(
    store: StateStore, plan: PostDeploymentAssertionPlan
) -> DeploymentPromotion | None:
    """Load immutable publication evidence after re-proving success authority."""
    try:
        prepared, finalization = _authority(store, plan)
        _ensure_table(store)
        rows = store._connection.execute(
            "SELECT deployment_id,target,repository_id,candidate_sha,prior_main_sha,"
            "finalization_sha256,backup_slug,tag,phase,recorded_at,completed_at,record_sha256 "
            "FROM deployment_promotion WHERE deployment_id = ?",
            (prepared.deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 12:
            _invalid()
        row = rows[0]
        recorded = _parse_time(row[9])
        completed = None if row[10] is None else _parse_time(row[10])
        value = DeploymentPromotion(
            str(row[0]), str(row[1]), row[2], str(row[3]), str(row[4]), str(row[5]),
            str(row[6]), str(row[7]), str(row[8]), recorded, completed, str(row[11])
        )
        value.validate()
        if (
            value.target != prepared.evidence.target
            or value.repository_id != prepared.evidence.repository_id
            or value.candidate_sha != prepared.evidence.candidate_sha
            or value.prior_main_sha != prepared.evidence.baseline_sha
            or value.backup_slug != prepared.evidence.backup_slug
            or value.finalization_sha256 != finalization.record_sha256
            or store.repository_id(value.target) != value.repository_id
        ):
            _invalid()
        return value
    except DeploymentPromotionError:
        raise
    except (DeploymentFinalizationError, PreparedDeploymentError, StateError, sqlite3.Error, ValueError, AttributeError):
        _invalid()


def _authority(store: StateStore, plan: PostDeploymentAssertionPlan):
    plan._validate()
    deployment_id = plan.automation_target.resource_target.deployment_id
    prepared = store.prepared_deployment(deployment_id)
    finalization = load_deployment_finalization(store, plan)
    if (
        prepared is None
        or finalization is None
        or finalization.outcome != "success"
        or candidate_finalization_authority(store, plan) != "promote_and_tag"
        or finalization.candidate_sha != prepared.evidence.candidate_sha
        or finalization.backup_slug != prepared.evidence.backup_slug
        or store.repository_id(prepared.evidence.target) != prepared.evidence.repository_id
    ):
        _invalid()
    return prepared, finalization


def _new_intent(prepared, finalization_sha256: str, when: datetime) -> DeploymentPromotion:
    tag = f"syncapp-known-good-{prepared.deployment_id}"
    values = (
        prepared.deployment_id,
        prepared.evidence.target,
        prepared.evidence.repository_id,
        prepared.evidence.candidate_sha,
        prepared.evidence.baseline_sha,
        finalization_sha256,
        prepared.evidence.backup_slug,
        tag,
        "planned",
        when.isoformat(),
        None,
    )
    return DeploymentPromotion(*values[:9], when, None, _digest(values))


def _persist(store: StateStore, value: DeploymentPromotion) -> None:
    try:
        _ensure_table(store)
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO deployment_promotion VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                value.database_values(),
            )
    except sqlite3.Error:
        _invalid()


def _complete(
    store: StateStore, value: DeploymentPromotion, when: datetime, *, replayed: bool
) -> DeploymentPromotionResult:
    completed_values = (
        value.deployment_id, value.target, value.repository_id, value.candidate_sha,
        value.prior_main_sha, value.finalization_sha256, value.backup_slug, value.tag,
        "completed", value.recorded_at.isoformat(), when.isoformat(),
    )
    completed = DeploymentPromotion(
        value.deployment_id, value.target, value.repository_id, value.candidate_sha,
        value.prior_main_sha, value.finalization_sha256, value.backup_slug, value.tag,
        "completed", value.recorded_at, when, _digest(completed_values),
    )
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE deployment_promotion SET phase='completed',completed_at=?,record_sha256=? "
                "WHERE deployment_id=? AND phase='planned' AND record_sha256=?",
                (when.isoformat(), completed.record_sha256, value.deployment_id, value.record_sha256),
            )
            if result.rowcount != 1:
                _invalid()
    except sqlite3.Error:
        _invalid()
    return _result(completed, replayed)


def _block(store: StateStore, value: DeploymentPromotion) -> None:
    blocked_values = (*value.values_without_digest()[:8], "blocked", value.recorded_at.isoformat(), None)
    digest = _digest(blocked_values)
    with store._connection as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE deployment_promotion SET phase='blocked',record_sha256=? "
            "WHERE deployment_id=? AND phase='planned' AND record_sha256=?",
            (digest, value.deployment_id, value.record_sha256),
        )


def _ensure_table(store: StateStore) -> None:
    store._connection.execute(
        "CREATE TABLE IF NOT EXISTS deployment_promotion ("
        "deployment_id TEXT PRIMARY KEY NOT NULL,target TEXT NOT NULL,"
        "repository_id INTEGER NOT NULL CHECK(repository_id>0),candidate_sha TEXT NOT NULL,"
        "prior_main_sha TEXT NOT NULL,finalization_sha256 TEXT NOT NULL,backup_slug TEXT NOT NULL,"
        "tag TEXT NOT NULL,phase TEXT NOT NULL CHECK(phase IN ('planned','completed','blocked')),"
        "recorded_at TEXT NOT NULL,completed_at TEXT,record_sha256 TEXT NOT NULL)"
    )


def _result(value: DeploymentPromotion, replayed: bool) -> DeploymentPromotionResult:
    return DeploymentPromotionResult("completed", replayed, value.tag, value.candidate_sha)


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(UTC)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        _invalid()
    parsed = datetime.fromisoformat(value)
    return _timestamp(parsed)


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _invalid() -> NoReturn:
    raise DeploymentPromotionError("Promotion state is invalid or unauthorized")
