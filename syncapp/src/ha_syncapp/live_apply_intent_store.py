"""Crash-safe, non-authoritative persistence for verified live Apply intents."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import NoReturn

from .apply_authorization import ApplyAuthorization
from .live_apply_intent import LiveApplyIntent, derive_live_apply_intent
from .live_apply_plan import LiveApplyPlan
from .live_apply_preconditions import LiveApplyPreconditionEvidence
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .stage_prewrite_reproof import StagePrewriteEvidence
from .state import StateError, StateStore

_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_DISCOVERABLE_INTENTS = 4096


@dataclass(frozen=True, slots=True)
class PersistedLiveApplyIntent:
    """Durable recovery metadata that never authorizes live mutation by itself."""

    deployment_id: str
    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    backup_slug: str
    homeassistant_root: str
    operations_sha256: str
    recorded_at: datetime
    record_sha256: str

    @classmethod
    def from_intent(
        cls,
        intent: LiveApplyIntent,
        recorded_at: datetime,
    ) -> PersistedLiveApplyIntent:
        when = _timestamp(recorded_at)
        digest_values: tuple[object, ...] = (
            intent.deployment_id,
            intent.target,
            intent.repository_id,
            intent.baseline_sha,
            intent.candidate_sha,
            intent.stage_manifest_sha256,
            intent.backup_slug,
            intent.homeassistant_root,
            intent.operations_sha256,
            when.isoformat(),
        )
        return cls(
            deployment_id=intent.deployment_id,
            target=intent.target,
            repository_id=intent.repository_id,
            baseline_sha=intent.baseline_sha,
            candidate_sha=intent.candidate_sha,
            stage_manifest_sha256=intent.stage_manifest_sha256,
            backup_slug=intent.backup_slug,
            homeassistant_root=intent.homeassistant_root,
            operations_sha256=intent.operations_sha256,
            recorded_at=when,
            record_sha256=_record_digest(digest_values),
        )

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> PersistedLiveApplyIntent:
        if len(row) != 11:
            _invalid_record()
        deployment_id = _text(row[0])
        target = _text(row[1])
        repository_id = row[2]
        baseline_sha = _text(row[3])
        candidate_sha = _text(row[4])
        stage_manifest_sha256 = _text(row[5])
        backup_slug = _text(row[6])
        homeassistant_root = _text(row[7])
        operations_sha256 = _text(row[8])
        recorded_at_text = _text(row[9])
        record_sha256 = _text(row[10])
        if type(repository_id) is not int:
            _invalid_record()
        try:
            recorded_at = datetime.fromisoformat(recorded_at_text)
        except ValueError:
            _invalid_record()
        record = cls(
            deployment_id=deployment_id,
            target=target,
            repository_id=repository_id,
            baseline_sha=baseline_sha,
            candidate_sha=candidate_sha,
            stage_manifest_sha256=stage_manifest_sha256,
            backup_slug=backup_slug,
            homeassistant_root=homeassistant_root,
            operations_sha256=operations_sha256,
            recorded_at=recorded_at,
            record_sha256=record_sha256,
        )
        record._validate()
        if record.database_values() != row:
            _invalid_record()
        return record

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        when = self.recorded_at.astimezone(UTC).isoformat()
        values = (*astuple(self)[:9], when)
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
            or not _canonical_root(self.homeassistant_root)
            or _HASH.fullmatch(self.operations_sha256) is None
            or not isinstance(self.recorded_at, datetime)
            or self.recorded_at.tzinfo is None
            or self.recorded_at.utcoffset() is None
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_record()


def record_live_apply_intent(
    store: StateStore,
    authorization: ApplyAuthorization,
    stage_evidence: StagePrewriteEvidence,
    plan: LiveApplyPlan,
    preconditions: LiveApplyPreconditionEvidence,
    *,
    recorded_at: datetime | None = None,
) -> PersistedLiveApplyIntent:
    """Persist one exact verified chain, idempotently, before any live mutation."""
    if type(store) is not StateStore:
        raise StateError("Invalid live Apply intent store")
    try:
        intent = derive_live_apply_intent(authorization, stage_evidence, plan, preconditions)
    except Exception:
        raise StateError("Live Apply intent evidence chain is invalid") from None
    _revalidate_prepared_binding(store, intent)
    when = _timestamp(recorded_at)
    expected = PersistedLiveApplyIntent.from_intent(intent, when)
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            _revalidate_prepared_binding(store, intent)
            existing = _select_intent_row(db, intent.deployment_id)
            if existing is not None:
                persisted = _parse_and_revalidate(store, existing)
                if not _same_intent(persisted, expected):
                    raise StateError("Live Apply intent cannot be rebound")
                return persisted
            try:
                db.execute(
                    "INSERT INTO live_apply_intent (deployment_id, target, repository_id, "
                    "baseline_sha, candidate_sha, stage_manifest_sha256, backup_slug, "
                    "homeassistant_root, operations_sha256, recorded_at, record_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    expected.database_values(),
                )
            except sqlite3.IntegrityError:
                raise StateError("Live Apply intent cannot be rebound") from None
        loaded = load_live_apply_intent(store, intent.deployment_id)
        if loaded is None or not _same_intent(loaded, expected):
            raise StateError("Live Apply intent was not persisted")
        return loaded
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to persist live Apply intent") from None


def load_live_apply_intent(
    store: StateStore,
    deployment_id: str,
) -> PersistedLiveApplyIntent | None:
    """Load one intent while rechecking integrity, repository and prepared evidence."""
    if type(store) is not StateStore:
        raise StateError("Invalid live Apply intent store")
    try:
        validate_deployment_id(deployment_id)
    except PreparedDeploymentError:
        raise StateError("Invalid live Apply intent identity") from None
    try:
        row = _select_intent_row(store._connection, deployment_id)
        if row is None:
            return None
        return _parse_and_revalidate(store, row)
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to read live Apply intent") from None


def discover_live_apply_intents(store: StateStore) -> tuple[PersistedLiveApplyIntent, ...]:
    """Discover bounded durable intents for retrigger recovery; return no write authority."""
    if type(store) is not StateStore:
        raise StateError("Invalid live Apply intent store")
    try:
        rows = store._connection.execute(
            "SELECT deployment_id FROM live_apply_intent "
            "ORDER BY recorded_at, deployment_id LIMIT ?",
            (_MAX_DISCOVERABLE_INTENTS + 1,),
        ).fetchall()
        if len(rows) > _MAX_DISCOVERABLE_INTENTS:
            raise StateError("Live Apply intent discovery exceeds the limit")
        result: list[PersistedLiveApplyIntent] = []
        for row in rows:
            if len(row) != 1 or not isinstance(row[0], str):
                _invalid_record()
            intent = load_live_apply_intent(store, row[0])
            if intent is None:
                _invalid_record()
            result.append(intent)
        return tuple(result)
    except StateError:
        raise
    except sqlite3.Error:
        raise StateError("Unable to discover live Apply intents") from None


def _select_intent_row(
    db: sqlite3.Connection,
    deployment_id: str,
) -> tuple[object, ...] | None:
    rows = db.execute(
        "SELECT deployment_id, target, repository_id, baseline_sha, candidate_sha, "
        "stage_manifest_sha256, backup_slug, homeassistant_root, operations_sha256, "
        "recorded_at, record_sha256 FROM live_apply_intent WHERE deployment_id = ?",
        (deployment_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        _invalid_record()
    row: tuple[object, ...] = tuple(rows[0])
    return row


def _parse_and_revalidate(
    store: StateStore,
    row: tuple[object, ...],
) -> PersistedLiveApplyIntent:
    try:
        record = PersistedLiveApplyIntent.from_database_row(row)
        if store.repository_id(record.target) != record.repository_id:
            _invalid_record()
        prepared = store.prepared_deployment(record.deployment_id)
        if prepared is None:
            _invalid_record()
        evidence = prepared.evidence
        if (
            record.target != evidence.target
            or record.repository_id != evidence.repository_id
            or record.baseline_sha != evidence.baseline_sha
            or record.candidate_sha != evidence.candidate_sha
            or record.stage_manifest_sha256 != evidence.stage_manifest_sha256
            or record.backup_slug != evidence.backup_slug
        ):
            _invalid_record()
        return record
    except StateError:
        raise
    except Exception:
        _invalid_record()


def _revalidate_prepared_binding(store: StateStore, intent: LiveApplyIntent) -> None:
    try:
        prepared = store.prepared_deployment(intent.deployment_id)
    except StateError:
        raise
    if prepared is None:
        raise StateError("Live Apply intent requires matching prepared deployment")
    evidence = prepared.evidence
    if (
        store.repository_id(intent.target) != intent.repository_id
        or intent.target != evidence.target
        or intent.repository_id != evidence.repository_id
        or intent.baseline_sha != evidence.baseline_sha
        or intent.candidate_sha != evidence.candidate_sha
        or intent.stage_manifest_sha256 != evidence.stage_manifest_sha256
        or intent.backup_slug != evidence.backup_slug
    ):
        raise StateError("Live Apply intent prepared deployment binding mismatch")


def _same_intent(
    left: PersistedLiveApplyIntent,
    right: PersistedLiveApplyIntent,
) -> bool:
    return astuple(left)[:9] == astuple(right)[:9]


def _timestamp(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise StateError("Live Apply intent timestamp must include a timezone")
    return current.astimezone(UTC)


def _canonical_root(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        return False
    root = PurePosixPath(value)
    return str(root) == value and not any(part in {".", ".."} for part in root.parts)


def _record_digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid_record()
    return value


def _invalid_record() -> NoReturn:
    raise StateError("Invalid live Apply intent record") from None
