"""Crash-safe authority and orchestration for known-good Git promotion."""

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
    load_deployment_finalization,
)
from .post_deployment_assertion_observation import (
    PostDeploymentAssertionObservationError,
    PostDeploymentAssertionPlan,
)
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .state import StateError, StateStore

_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TAG = re.compile(r"^syncapp-known-good-[0-9a-f-]{36}$")
_TOKEN = re.compile(r"^[!-~]{1,512}$")
_MAX_DISCOVERABLE_PROMOTIONS = 64


class DeploymentPromotionError(RuntimeError):
    """A known-good promotion could not proceed safely."""


@dataclass(frozen=True, slots=True)
class PromotionRemoteState:
    candidate_sha: str
    main_sha: str
    tag_sha: str | None

    def validate(self) -> None:
        if (
            _COMMIT.fullmatch(self.candidate_sha) is None
            or _COMMIT.fullmatch(self.main_sha) is None
            or (self.tag_sha is not None and _COMMIT.fullmatch(self.tag_sha) is None)
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class DeploymentPromotion:
    deployment_id: str
    target: str
    repository_id: int
    candidate_sha: str
    baseline_sha: str
    backup_slug: str
    finalization_sha256: str
    known_good_tag: str
    phase: str
    block_reason: str
    planned_at: datetime
    terminal_at: datetime | None
    record_sha256: str

    @classmethod
    def create(
        cls,
        *,
        deployment_id: str,
        target: str,
        repository_id: int,
        candidate_sha: str,
        baseline_sha: str,
        backup_slug: str,
        finalization_sha256: str,
        known_good_tag: str,
        phase: str,
        block_reason: str,
        planned_at: datetime,
        terminal_at: datetime | None,
    ) -> DeploymentPromotion:
        planned = _timestamp(planned_at)
        terminal = None if terminal_at is None else _timestamp(terminal_at)
        values: tuple[object, ...] = (
            deployment_id,
            target,
            repository_id,
            candidate_sha,
            baseline_sha,
            backup_slug,
            finalization_sha256,
            known_good_tag,
            phase,
            block_reason,
            planned.isoformat(),
            None if terminal is None else terminal.isoformat(),
        )
        result = cls(
            deployment_id,
            target,
            repository_id,
            candidate_sha,
            baseline_sha,
            backup_slug,
            finalization_sha256,
            known_good_tag,
            phase,
            block_reason,
            planned,
            terminal,
            _digest(values),
        )
        result.validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> DeploymentPromotion:
        if len(row) != 13 or type(row[2]) is not int:
            _invalid_state()
        try:
            planned = datetime.fromisoformat(_text(row[10]))
            terminal = None if row[11] is None else datetime.fromisoformat(_text(row[11]))
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
            planned,
            terminal,
            _text(row[12]),
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
            self.candidate_sha,
            self.baseline_sha,
            self.backup_slug,
            self.finalization_sha256,
            self.known_good_tag,
            self.phase,
            self.block_reason,
            self.planned_at.astimezone(UTC).isoformat(),
            None if self.terminal_at is None else self.terminal_at.astimezone(UTC).isoformat(),
        )
        return (*values, _digest(values))

    def validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        terminal = self.phase in {"completed", "blocked"}
        if (
            _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or _COMMIT.fullmatch(self.candidate_sha) is None
            or _COMMIT.fullmatch(self.baseline_sha) is None
            or self.candidate_sha == self.baseline_sha
            or _SLUG.fullmatch(self.backup_slug) is None
            or _HASH.fullmatch(self.finalization_sha256) is None
            or _TAG.fullmatch(self.known_good_tag) is None
            or self.phase not in {"planned", "completed", "blocked"}
            or self.block_reason not in {"none", "ref_divergence"}
            or (self.phase == "blocked") != (self.block_reason == "ref_divergence")
            or terminal != (self.terminal_at is not None)
            or self.planned_at.tzinfo is None
            or self.planned_at.utcoffset() is None
            or (self.terminal_at is not None and self.terminal_at < self.planned_at)
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class DeploymentPromotionResult:
    status: str
    replayed: bool
    candidate_sha: str
    known_good_tag: str


RemoteReader = Callable[[str, str, int, str], PromotionRemoteState]
Publisher = Callable[[DeploymentPromotion, PromotionRemoteState, str], None]


def promote_finalized_deployment_once(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    *,
    token: str | None,
    remote_reader: RemoteReader | None = None,
    publisher: Publisher | None = None,
    observed_at: datetime | None = None,
) -> DeploymentPromotionResult:
    """Plan durably, publish missing refs once, and reconcile uncertain outcomes."""
    try:
        existing = load_deployment_promotion(store, plan)
        if existing is not None and existing.phase == "completed":
            return _result(existing, True)
        if existing is not None and existing.phase == "blocked":
            _blocked()
        finalization = _successful_finalization(store, plan)
        prepared = store.prepared_deployment(finalization.deployment_id)
        if prepared is None:
            _invalid_state()
        prepared.validate()
        now = _timestamp(observed_at or datetime.now(UTC))
        if now < finalization.finalized_at:
            _invalid_state()
        if existing is None:
            requested = DeploymentPromotion.create(
                deployment_id=finalization.deployment_id,
                target=prepared.evidence.target,
                repository_id=prepared.evidence.repository_id,
                candidate_sha=finalization.candidate_sha,
                baseline_sha=prepared.evidence.baseline_sha,
                backup_slug=finalization.backup_slug,
                finalization_sha256=finalization.record_sha256,
                known_good_tag=f"syncapp-known-good-{finalization.deployment_id}",
                phase="planned",
                block_reason="none",
                planned_at=now,
                terminal_at=None,
            )
            existing = _insert_plan(store, plan, requested)
        credential = _validate_token(token)
        if remote_reader is None or publisher is None:
            from .deployment_promotion_transport import (
                publish_promotion_refs,
                read_promotion_remote_state,
            )

            remote_reader = remote_reader or read_promotion_remote_state
            publisher = publisher or publish_promotion_refs
        before = _read_remote(remote_reader, existing, credential)
        disposition = _disposition(existing, before)
        if disposition == "blocked":
            _set_terminal(store, plan, existing, "blocked", now)
            _blocked()
        if disposition == "completed":
            completed = _set_terminal(store, plan, existing, "completed", now)
            return _result(completed, False)
        try:
            publisher(existing, before, credential)
        except Exception:
            _unavailable()
        after = _read_remote(remote_reader, existing, credential)
        if _disposition(existing, after) != "completed":
            if _disposition(existing, after) == "blocked":
                _set_terminal(store, plan, existing, "blocked", now)
                _blocked()
            _unavailable()
        completed = _set_terminal(store, plan, existing, "completed", now)
        return _result(completed, False)
    except DeploymentPromotionError:
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


def load_deployment_promotion(
    store: StateStore, plan: PostDeploymentAssertionPlan
) -> DeploymentPromotion | None:
    try:
        plan._validate()
        deployment_id = plan.automation_target.resource_target.deployment_id
        rows = store._connection.execute(
            "SELECT deployment_id, target, repository_id, candidate_sha, baseline_sha, "
            "backup_slug, finalization_sha256, known_good_tag, phase, block_reason, "
            "planned_at, terminal_at, record_sha256 FROM deployment_promotion "
            "WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = DeploymentPromotion.from_database_row(tuple(rows[0]))
        finalization = _successful_finalization(store, plan)
        prepared = store.prepared_deployment(deployment_id)
        if prepared is None:
            _invalid_state()
        prepared.validate()
        if (
            result.target != prepared.evidence.target
            or result.repository_id != prepared.evidence.repository_id
            or result.candidate_sha != finalization.candidate_sha
            or result.baseline_sha != prepared.evidence.baseline_sha
            or result.backup_slug != finalization.backup_slug
            or result.finalization_sha256 != finalization.record_sha256
            or result.known_good_tag != f"syncapp-known-good-{deployment_id}"
            or result.planned_at < finalization.finalized_at
        ):
            _invalid_state()
        return result
    except DeploymentPromotionError:
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


def discover_pending_deployment_promotions(
    store: StateStore,
) -> tuple[DeploymentPromotion, ...]:
    """Expose bounded planned work to Retrigger without executing Git mutation."""
    try:
        rows = store._connection.execute(
            "SELECT deployment_id, target, repository_id, candidate_sha, baseline_sha, "
            "backup_slug, finalization_sha256, known_good_tag, phase, block_reason, "
            "planned_at, terminal_at, record_sha256 FROM deployment_promotion "
            "WHERE phase = 'planned' ORDER BY planned_at, deployment_id LIMIT ?",
            (_MAX_DISCOVERABLE_PROMOTIONS + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE_PROMOTIONS:
            raise DeploymentPromotionError("deployment promotion discovery exceeds the limit")
        return tuple(DeploymentPromotion.from_database_row(tuple(row)) for row in rows)
    except DeploymentPromotionError:
        raise
    except (StateError, sqlite3.Error):
        _invalid_state()


def _successful_finalization(
    store: StateStore, plan: PostDeploymentAssertionPlan
) -> DeploymentFinalization:
    finalization = load_deployment_finalization(store, plan)
    if finalization is None or finalization.outcome != "success":
        raise DeploymentPromotionError("successful deployment finalization is required")
    return finalization


def _insert_plan(
    store: StateStore, plan: PostDeploymentAssertionPlan, requested: DeploymentPromotion
) -> DeploymentPromotion:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _successful_finalization(store, plan)
            existing = load_deployment_promotion(store, plan)
            if existing is not None:
                return existing
            db.execute(
                "INSERT INTO deployment_promotion VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_deployment_promotion(store, plan)
        if loaded != requested:
            _invalid_state()
        return requested
    except DeploymentPromotionError:
        raise
    except (DeploymentFinalizationError, StateError, sqlite3.Error):
        _invalid_state()


def _set_terminal(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    current: DeploymentPromotion,
    phase: str,
    terminal_at: datetime,
) -> DeploymentPromotion:
    requested = DeploymentPromotion.create(
        deployment_id=current.deployment_id,
        target=current.target,
        repository_id=current.repository_id,
        candidate_sha=current.candidate_sha,
        baseline_sha=current.baseline_sha,
        backup_slug=current.backup_slug,
        finalization_sha256=current.finalization_sha256,
        known_good_tag=current.known_good_tag,
        phase=phase,
        block_reason="ref_divergence" if phase == "blocked" else "none",
        planned_at=current.planned_at,
        terminal_at=terminal_at,
    )
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            loaded = load_deployment_promotion(store, plan)
            if loaded is None:
                _invalid_state()
            if loaded.phase == phase:
                return loaded
            if loaded != current or loaded.phase != "planned":
                _invalid_state()
            changed = db.execute(
                "UPDATE deployment_promotion SET phase = ?, block_reason = ?, "
                "terminal_at = ?, record_sha256 = ? WHERE deployment_id = ? "
                "AND record_sha256 = ?",
                (
                    requested.phase,
                    requested.block_reason,
                    requested.terminal_at.isoformat() if requested.terminal_at else None,
                    requested.record_sha256,
                    requested.deployment_id,
                    current.record_sha256,
                ),
            ).rowcount
            if changed != 1:
                _invalid_state()
        loaded = load_deployment_promotion(store, plan)
        if loaded != requested:
            _invalid_state()
        return requested
    except DeploymentPromotionError:
        raise
    except (StateError, sqlite3.Error):
        _invalid_state()


def _read_remote(
    reader: RemoteReader, intent: DeploymentPromotion, token: str
) -> PromotionRemoteState:
    try:
        value = reader(intent.target, token, intent.repository_id, intent.known_good_tag)
        if type(value) is not PromotionRemoteState:
            _invalid_state()
        value.validate()
        return value
    except DeploymentPromotionError:
        raise
    except Exception:
        _unavailable()


def _disposition(intent: DeploymentPromotion, state: PromotionRemoteState) -> str:
    if state.candidate_sha != intent.candidate_sha:
        return "blocked"
    if state.main_sha not in {intent.baseline_sha, intent.candidate_sha}:
        return "blocked"
    if state.tag_sha not in {None, intent.candidate_sha}:
        return "blocked"
    if state.main_sha == intent.candidate_sha and state.tag_sha == intent.candidate_sha:
        return "completed"
    return "publish"


def _validate_token(token: str | None) -> str:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise DeploymentPromotionError("GitHub authentication is unavailable")
    return token


def _result(value: DeploymentPromotion, replayed: bool) -> DeploymentPromotionResult:
    return DeploymentPromotionResult(
        value.phase, replayed, value.candidate_sha, value.known_good_tag
    )


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        _invalid_state()
    return value.astimezone(UTC)


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid_state()
    return value


def _invalid_state() -> NoReturn:
    raise DeploymentPromotionError("deployment promotion state is invalid") from None


def _blocked() -> NoReturn:
    raise DeploymentPromotionError("deployment promotion is blocked") from None


def _unavailable() -> NoReturn:
    raise DeploymentPromotionError("deployment promotion transport is unavailable") from None
