"""Immutable deployment outcome and promotion/rollback decision authority."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .automation_script_observation import (
    AutomationScriptObservationError,
    load_automation_script_observation,
)
from .entity_state_observation import EntityStateObservationError, load_entity_state_observation
from .post_deployment_assertion_observation import (
    PostDeploymentAssertionObservationError,
    PostDeploymentAssertionPlan,
    load_post_deployment_assertion_observation,
)
from .prepared_deployment import (
    PreparedDeployment,
    PreparedDeploymentError,
    validate_deployment_id,
)
from .resource_availability_observation import (
    ResourceAvailabilityError,
    load_resource_availability_observation,
)
from .startup_error_observation import (
    StartupErrorObservationError,
    load_startup_error_observation,
)
from .state import StateError, StateStore

_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_OUTCOMES = {"success", "failure"}
_FAILURE_STAGES = {
    "none",
    "startup_errors",
    "entity_states",
    "automation_script_load",
    "post_deployment_assertions",
}
_DOWNSTREAM_QUERIES = {
    "resource_availability_observation": (
        "SELECT 1 FROM resource_availability_observation WHERE deployment_id = ? LIMIT 1"
    ),
    "entity_state_observation": (
        "SELECT 1 FROM entity_state_observation WHERE deployment_id = ? LIMIT 1"
    ),
    "automation_script_observation": (
        "SELECT 1 FROM automation_script_observation WHERE deployment_id = ? LIMIT 1"
    ),
    "post_deployment_assertion_observation": (
        "SELECT 1 FROM post_deployment_assertion_observation WHERE deployment_id = ? LIMIT 1"
    ),
}


class DeploymentFinalizationError(RuntimeError):
    """A deployment cannot be finalized from the available trusted evidence."""


@dataclass(frozen=True, slots=True, init=False)
class DeploymentFinalization:
    deployment_id: str
    candidate_sha: str
    backup_slug: str
    prepared_deployment_sha256: str
    target_sha256: str
    terminal_evidence_sha256: str
    chain_sha256: str
    outcome: str
    failure_stage: str
    completed_predicate_count: int
    failed_predicate_count: int
    finalized_at: datetime
    record_sha256: str

    @classmethod
    def create(
        cls,
        decision: _Decision,
        finalized_at: datetime,
    ) -> DeploymentFinalization:
        when = _timestamp(finalized_at)
        values: tuple[object, ...] = (
            decision.deployment_id,
            decision.candidate_sha,
            decision.backup_slug,
            decision.prepared_deployment_sha256,
            decision.target_sha256,
            decision.terminal_evidence_sha256,
            decision.chain_sha256,
            decision.outcome,
            decision.failure_stage,
            decision.completed_predicate_count,
            decision.failed_predicate_count,
            when.isoformat(),
        )
        result = object.__new__(cls)
        for name, value in zip(
            (
                "deployment_id",
                "candidate_sha",
                "backup_slug",
                "prepared_deployment_sha256",
                "target_sha256",
                "terminal_evidence_sha256",
                "chain_sha256",
                "outcome",
                "failure_stage",
                "completed_predicate_count",
                "failed_predicate_count",
                "finalized_at",
                "record_sha256",
            ),
            (*values[:11], when, _digest(values)),
            strict=True,
        ):
            object.__setattr__(result, name, value)
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> DeploymentFinalization:
        if len(row) != 13:
            _invalid_state()
        try:
            when = datetime.fromisoformat(_text(row[11]))
        except ValueError:
            _invalid_state()
        result = object.__new__(cls)
        values = (
            _text(row[0]),
            _text(row[1]),
            _text(row[2]),
            _text(row[3]),
            _text(row[4]),
            _text(row[5]),
            _text(row[6]),
            _text(row[7]),
            _text(row[8]),
            row[9],
            row[10],
            when,
            _text(row[12]),
        )
        for name, value in zip(cls.__slots__, values, strict=True):
            object.__setattr__(result, name, value)
        result._validate()
        if result.database_values() != row:
            _invalid_state()
        return result

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        values: tuple[object, ...] = (
            self.deployment_id,
            self.candidate_sha,
            self.backup_slug,
            self.prepared_deployment_sha256,
            self.target_sha256,
            self.terminal_evidence_sha256,
            self.chain_sha256,
            self.outcome,
            self.failure_stage,
            self.completed_predicate_count,
            self.failed_predicate_count,
            self.finalized_at.astimezone(UTC).isoformat(),
        )
        return (*values, _digest(values))

    def _validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        success = self.outcome == "success"
        expected_count = {
            "none": 5,
            "startup_errors": 1,
            "entity_states": 3,
            "automation_script_load": 4,
            "post_deployment_assertions": 5,
        }.get(self.failure_stage)
        if (
            _COMMIT.fullmatch(self.candidate_sha) is None
            or _SLUG.fullmatch(self.backup_slug) is None
            or any(
                _HASH.fullmatch(value) is None
                for value in (
                    self.prepared_deployment_sha256,
                    self.target_sha256,
                    self.terminal_evidence_sha256,
                    self.chain_sha256,
                    self.record_sha256,
                )
            )
            or self.outcome not in _OUTCOMES
            or self.failure_stage not in _FAILURE_STAGES
            or type(self.completed_predicate_count) is not int
            or not 1 <= self.completed_predicate_count <= 5
            or type(self.failed_predicate_count) is not int
            or self.failed_predicate_count not in {0, 1}
            or success != (self.failure_stage == "none")
            or success != (self.failed_predicate_count == 0)
            or self.completed_predicate_count != expected_count
            or self.finalized_at.tzinfo is None
            or self.finalized_at.utcoffset() is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class DeploymentFinalizationResult:
    outcome: str
    authority: str
    failure_stage: str
    candidate_blocked: bool
    replayed: bool
    completed_predicate_count: int


@dataclass(frozen=True, slots=True)
class _Decision:
    deployment_id: str
    candidate_sha: str
    backup_slug: str
    prepared_deployment_sha256: str
    target_sha256: str
    terminal_evidence_sha256: str
    chain_sha256: str
    outcome: str
    failure_stage: str
    completed_predicate_count: int
    failed_predicate_count: int
    terminal_at: datetime


def finalize_deployment_once(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    *,
    finalized_at: datetime | None = None,
) -> DeploymentFinalizationResult:
    """Persist exactly one outcome; this grants authority but performs no action."""
    try:
        existing = load_deployment_finalization(store, plan)
        if existing is not None:
            return _result(existing, True)
        decision = _decision(store, plan)
        requested = DeploymentFinalization.create(
            decision, _timestamp(finalized_at or datetime.now(UTC))
        )
        if requested.finalized_at < decision.terminal_at:
            _invalid_state()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            decision = _decision(store, plan)
            if requested != DeploymentFinalization.create(decision, requested.finalized_at):
                _invalid_state()
            existing = load_deployment_finalization(store, plan)
            if existing is not None:
                return _result(existing, True)
            db.execute(
                "INSERT INTO deployment_finalization VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_deployment_finalization(store, plan)
        if loaded != requested:
            _invalid_state()
        return _result(requested, False)
    except DeploymentFinalizationError:
        raise
    except (
        AutomationScriptObservationError,
        EntityStateObservationError,
        PostDeploymentAssertionObservationError,
        PreparedDeploymentError,
        ResourceAvailabilityError,
        StartupErrorObservationError,
        StateError,
        sqlite3.Error,
        AttributeError,
    ):
        _invalid_state()


def load_deployment_finalization(
    store: StateStore, plan: PostDeploymentAssertionPlan
) -> DeploymentFinalization | None:
    """Load an outcome only after re-proving its complete decision chain."""
    try:
        plan._validate()
        deployment_id = plan.automation_target.resource_target.deployment_id
        rows = store._connection.execute(
            "SELECT deployment_id, candidate_sha, backup_slug, "
            "prepared_deployment_sha256, target_sha256, terminal_evidence_sha256, "
            "chain_sha256, outcome, failure_stage, completed_predicate_count, "
            "failed_predicate_count, finalized_at, record_sha256 "
            "FROM deployment_finalization WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = DeploymentFinalization.from_database_row(tuple(rows[0]))
        decision = _decision(store, plan)
        expected = DeploymentFinalization.create(decision, result.finalized_at)
        if result != expected or result.finalized_at < decision.terminal_at:
            _invalid_state()
        return result
    except DeploymentFinalizationError:
        raise
    except (
        AutomationScriptObservationError,
        EntityStateObservationError,
        PostDeploymentAssertionObservationError,
        PreparedDeploymentError,
        ResourceAvailabilityError,
        StartupErrorObservationError,
        StateError,
        sqlite3.Error,
        AttributeError,
    ):
        _invalid_state()


def candidate_finalization_authority(store: StateStore, plan: PostDeploymentAssertionPlan) -> str:
    """Return the only action authorized by a durable finalization record."""
    record = load_deployment_finalization(store, plan)
    if record is None:
        return "none"
    return "promote_and_tag" if record.outcome == "success" else "rollback"


def is_candidate_blocked_by_finalization(
    store: StateStore, plan: PostDeploymentAssertionPlan, candidate_sha: str
) -> bool:
    """Report whether the exact candidate has a durable failed finalization."""
    if not isinstance(candidate_sha, str) or _COMMIT.fullmatch(candidate_sha) is None:
        _invalid_state()
    record = load_deployment_finalization(store, plan)
    return bool(
        record is not None and record.outcome == "failure" and record.candidate_sha == candidate_sha
    )


def _decision(store: StateStore, plan: PostDeploymentAssertionPlan) -> _Decision:
    plan._validate()
    target = plan.automation_target.resource_target
    prepared = store.prepared_deployment(target.deployment_id)
    if prepared is None:
        _invalid_state()
    prepared.validate()
    if prepared.evidence.candidate_sha != target.candidate_sha:
        _invalid_state()
    prepared_sha = _text(prepared.database_values()[-1])
    digests = [prepared_sha]

    startup = load_startup_error_observation(store, target.deployment_id)
    if startup is None:
        _incomplete()
    digests.append(startup.record_sha256)
    if startup.significant_error_count:
        _require_absent(
            store,
            target.deployment_id,
            (
                "resource_availability_observation",
                "entity_state_observation",
                "automation_script_observation",
                "post_deployment_assertion_observation",
            ),
        )
        return _make_decision(
            prepared, target.target_sha256, digests, startup.observed_at, "startup_errors", 1
        )

    availability = load_resource_availability_observation(store, target)
    if availability is None:
        _incomplete()
    digests.append(availability.record_sha256)

    entity = load_entity_state_observation(store, target)
    if entity is None:
        _incomplete()
    digests.append(entity.record_sha256)
    if entity.invalid_count:
        _require_absent(
            store,
            target.deployment_id,
            ("automation_script_observation", "post_deployment_assertion_observation"),
        )
        return _make_decision(
            prepared, target.target_sha256, digests, entity.observed_at, "entity_states", 3
        )

    automation = load_automation_script_observation(store, plan.automation_target)
    if automation is None:
        _incomplete()
    digests.append(automation.record_sha256)
    if automation.failed_count:
        _require_absent(store, target.deployment_id, ("post_deployment_assertion_observation",))
        return _make_decision(
            prepared,
            target.target_sha256,
            digests,
            automation.observed_at,
            "automation_script_load",
            4,
        )

    assertion = load_post_deployment_assertion_observation(store, plan)
    if assertion is None:
        _incomplete()
    digests.append(assertion.record_sha256)
    return _make_decision(
        prepared,
        target.target_sha256,
        digests,
        assertion.observed_at,
        "post_deployment_assertions" if assertion.failed_count else "none",
        5,
    )


def _make_decision(
    prepared: PreparedDeployment,
    target_sha256: str,
    digests: list[str],
    terminal_at: datetime,
    failure_stage: str,
    count: int,
) -> _Decision:
    failed = failure_stage != "none"
    return _Decision(
        prepared.deployment_id,
        prepared.evidence.candidate_sha,
        prepared.evidence.backup_slug,
        _text(prepared.database_values()[-1]),
        target_sha256,
        digests[-1],
        _digest(tuple(digests)),
        "failure" if failed else "success",
        failure_stage,
        count,
        int(failed),
        terminal_at,
    )


def _require_absent(store: StateStore, deployment_id: str, tables: tuple[str, ...]) -> None:
    for table in tables:
        query = _DOWNSTREAM_QUERIES.get(table)
        if (
            query is None
            or store._connection.execute(query, (deployment_id,)).fetchone() is not None
        ):
            _invalid_state()


def _result(value: DeploymentFinalization, replayed: bool) -> DeploymentFinalizationResult:
    return DeploymentFinalizationResult(
        value.outcome,
        "promote_and_tag" if value.outcome == "success" else "rollback",
        value.failure_stage,
        value.outcome == "failure",
        replayed,
        value.completed_predicate_count,
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


def _incomplete() -> NoReturn:
    raise DeploymentFinalizationError("deployment finalization evidence is incomplete")


def _invalid_state() -> NoReturn:
    raise DeploymentFinalizationError("deployment finalization state is invalid") from None
