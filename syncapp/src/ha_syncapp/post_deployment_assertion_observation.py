"""Bounded post-deployment assertions derived from trusted affected resources."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .automation_script_observation import (
    AutomationScriptObservation,
    AutomationScriptObservationError,
    AutomationScriptTarget,
    load_automation_script_observation,
)
from .integration_observation import SessionFactory
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id
from .resource_availability_observation import ResourceAvailabilityError, _probe_states
from .state import StateError, StateStore

ASSERTION_SCHEMA_VERSION = 1
MAX_ASSERTIONS = 512
MAX_ASSERTION_SET_BYTES = 64 * 1024
_ASSERTION_KIND = "entity_available"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_INVALID_STATES = {"unknown", "unavailable"}


class PostDeploymentAssertionObservationError(RuntimeError):
    """Post-deployment assertions could not be evaluated safely."""


@dataclass(frozen=True, slots=True, init=False)
class PostDeploymentAssertion:
    """One immutable assertion; instances are created only by trusted derivation."""

    kind: str
    entity_id: str

    def _validate(self) -> None:
        if self.kind != _ASSERTION_KIND or not isinstance(self.entity_id, str):
            _invalid_state()


def _assertion(entity_id: str) -> PostDeploymentAssertion:
    result = object.__new__(PostDeploymentAssertion)
    object.__setattr__(result, "kind", _ASSERTION_KIND)
    object.__setattr__(result, "entity_id", entity_id)
    result._validate()
    return result


@dataclass(frozen=True, slots=True, init=False)
class PostDeploymentAssertionPlan:
    """Canonical assertion authority derived from an exact affected-resource target."""

    schema_version: int
    automation_target: AutomationScriptTarget
    assertions: tuple[PostDeploymentAssertion, ...]
    canonical_json: str
    assertion_set_sha256: str

    def _validate(self) -> None:
        try:
            self.automation_target._validate()
        except AutomationScriptObservationError:
            _invalid_state()
        expected = tuple(
            _assertion(entity) for entity in self.automation_target.resource_target.entity_ids
        )
        canonical = _canonical(expected)
        if (
            self.schema_version != ASSERTION_SCHEMA_VERSION
            or self.assertions != expected
            or len(self.assertions) > MAX_ASSERTIONS
            or len(canonical.encode("ascii")) > MAX_ASSERTION_SET_BYTES
            or self.canonical_json != canonical
            or _HASH.fullmatch(self.assertion_set_sha256) is None
            or self.assertion_set_sha256 != _digest_text(canonical)
        ):
            _invalid_state()


def derive_post_deployment_assertion_plan(
    automation_target: AutomationScriptTarget,
) -> PostDeploymentAssertionPlan:
    """Derive a closed assertion set without accepting caller-selected resources."""
    try:
        automation_target._validate()
    except AutomationScriptObservationError:
        _invalid_state()
    assertions = tuple(
        _assertion(entity) for entity in automation_target.resource_target.entity_ids
    )
    canonical = _canonical(assertions)
    result = object.__new__(PostDeploymentAssertionPlan)
    object.__setattr__(result, "schema_version", ASSERTION_SCHEMA_VERSION)
    object.__setattr__(result, "automation_target", automation_target)
    object.__setattr__(result, "assertions", assertions)
    object.__setattr__(result, "canonical_json", canonical)
    object.__setattr__(result, "assertion_set_sha256", _digest_text(canonical))
    result._validate()
    return result


@dataclass(frozen=True, slots=True, init=False)
class PostDeploymentAssertionObservation:
    deployment_id: str
    automation_script_observation_sha256: str
    target_sha256: str
    assertion_set_sha256: str
    observed_at: datetime
    declared_count: int
    derived_count: int
    evaluated_count: int
    passed_count: int
    failed_count: int
    skipped_count: int
    record_sha256: str

    @classmethod
    def create(
        cls,
        deployment_id: str,
        automation_script_observation_sha256: str,
        target_sha256: str,
        assertion_set_sha256: str,
        observed_at: datetime,
        declared_count: int,
        derived_count: int,
        evaluated_count: int,
        passed_count: int,
        failed_count: int,
        skipped_count: int,
    ) -> PostDeploymentAssertionObservation:
        when = _timestamp(observed_at)
        values: tuple[object, ...] = (
            deployment_id,
            automation_script_observation_sha256,
            target_sha256,
            assertion_set_sha256,
            when.isoformat(),
            declared_count,
            derived_count,
            evaluated_count,
            passed_count,
            failed_count,
            skipped_count,
        )
        result = object.__new__(cls)
        names = (
            "deployment_id",
            "automation_script_observation_sha256",
            "target_sha256",
            "assertion_set_sha256",
            "observed_at",
            "declared_count",
            "derived_count",
            "evaluated_count",
            "passed_count",
            "failed_count",
            "skipped_count",
            "record_sha256",
        )
        assigned = (*values[:4], when, *values[5:], _digest(values))
        for name, value in zip(names, assigned, strict=True):
            object.__setattr__(result, name, value)
        result._validate()
        return result

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> PostDeploymentAssertionObservation:
        if len(row) != 12:
            _invalid_state()
        try:
            when = datetime.fromisoformat(_text(row[4]))
        except ValueError:
            _invalid_state()
        result = object.__new__(cls)
        names = (
            "deployment_id",
            "automation_script_observation_sha256",
            "target_sha256",
            "assertion_set_sha256",
            "observed_at",
            "declared_count",
            "derived_count",
            "evaluated_count",
            "passed_count",
            "failed_count",
            "skipped_count",
            "record_sha256",
        )
        assigned = (
            _text(row[0]),
            _text(row[1]),
            _text(row[2]),
            _text(row[3]),
            when,
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            _text(row[11]),
        )
        for name, value in zip(names, assigned, strict=True):
            object.__setattr__(result, name, value)
        result._validate()
        if result.database_values() != row:
            _invalid_state()
        return result

    def database_values(self) -> tuple[object, ...]:
        self._validate()
        values: tuple[object, ...] = (
            self.deployment_id,
            self.automation_script_observation_sha256,
            self.target_sha256,
            self.assertion_set_sha256,
            self.observed_at.astimezone(UTC).isoformat(),
            self.declared_count,
            self.derived_count,
            self.evaluated_count,
            self.passed_count,
            self.failed_count,
            self.skipped_count,
        )
        return (*values, _digest(values))

    def _validate(self) -> None:
        counts = (
            self.declared_count,
            self.derived_count,
            self.evaluated_count,
            self.passed_count,
            self.failed_count,
            self.skipped_count,
        )
        try:
            validate_deployment_id(self.deployment_id)
        except PreparedDeploymentError:
            _invalid_state()
        if (
            _HASH.fullmatch(self.automation_script_observation_sha256) is None
            or _HASH.fullmatch(self.target_sha256) is None
            or _HASH.fullmatch(self.assertion_set_sha256) is None
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
            or any(type(value) is not int or value < 0 for value in counts)
            or self.declared_count != 0
            or self.derived_count > MAX_ASSERTIONS
            or self.evaluated_count != self.passed_count + self.failed_count
            or self.evaluated_count + self.skipped_count != self.declared_count + self.derived_count
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid_state()


@dataclass(frozen=True, slots=True)
class PostDeploymentAssertionResult:
    status: str
    replayed: bool
    declared_count: int
    derived_count: int
    evaluated_count: int
    passed_count: int
    failed_count: int
    skipped_count: int


def evaluate_post_deployment_assertions_once(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    *,
    token: str | None = None,
    timeout_seconds: float = 10.0,
    max_message_bytes: int = 4 * 1024 * 1024,
    session_factory: SessionFactory | None = None,
    observed_at: datetime | None = None,
) -> PostDeploymentAssertionResult:
    plan._validate()
    prerequisite = _prerequisite(store, plan)
    existing = load_post_deployment_assertion_observation(store, plan)
    if existing is not None:
        return _result(existing, True)

    passed = 0
    failed = 0
    if plan.assertions:
        try:
            states = _probe_states(token, timeout_seconds, max_message_bytes, session_factory)
        except ResourceAvailabilityError:
            _unavailable()
        for assertion in plan.assertions:
            matches = [
                item
                for item in states
                if type(item) is dict and item.get("entity_id") == assertion.entity_id
            ]
            if (
                len(matches) == 1
                and type(matches[0].get("state")) is str
                and bool(matches[0].get("state"))
                and matches[0].get("state") not in _INVALID_STATES
            ):
                passed += 1
            else:
                failed += 1

    requested = PostDeploymentAssertionObservation.create(
        plan.automation_target.resource_target.deployment_id,
        prerequisite.record_sha256,
        plan.automation_target.resource_target.target_sha256,
        plan.assertion_set_sha256,
        _timestamp(observed_at),
        0,
        len(plan.assertions),
        len(plan.assertions),
        passed,
        failed,
        0,
    )
    if requested.observed_at < prerequisite.observed_at:
        _invalid_state()
    return _record(store, plan, requested)


def load_post_deployment_assertion_observation(
    store: StateStore, plan: PostDeploymentAssertionPlan
) -> PostDeploymentAssertionObservation | None:
    try:
        plan._validate()
        prerequisite = _prerequisite(store, plan)
        deployment_id = plan.automation_target.resource_target.deployment_id
        rows = store._connection.execute(
            "SELECT deployment_id, automation_script_observation_sha256, target_sha256, "
            "assertion_set_sha256, observed_at, declared_count, derived_count, "
            "evaluated_count, passed_count, failed_count, skipped_count, record_sha256 "
            "FROM post_deployment_assertion_observation WHERE deployment_id = ?",
            (deployment_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _invalid_state()
        result = PostDeploymentAssertionObservation.from_database_row(tuple(rows[0]))
        if (
            result.automation_script_observation_sha256 != prerequisite.record_sha256
            or result.target_sha256 != plan.automation_target.resource_target.target_sha256
            or result.assertion_set_sha256 != plan.assertion_set_sha256
            or result.derived_count != len(plan.assertions)
            or result.observed_at < prerequisite.observed_at
        ):
            _invalid_state()
        return result
    except PostDeploymentAssertionObservationError:
        raise
    except (
        AutomationScriptObservationError,
        ResourceAvailabilityError,
        StateError,
        sqlite3.Error,
    ):
        _invalid_state()


def _record(
    store: StateStore,
    plan: PostDeploymentAssertionPlan,
    requested: PostDeploymentAssertionObservation,
) -> PostDeploymentAssertionResult:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            prerequisite = _prerequisite(store, plan)
            if (
                requested.automation_script_observation_sha256 != prerequisite.record_sha256
                or requested.target_sha256 != plan.automation_target.resource_target.target_sha256
                or requested.assertion_set_sha256 != plan.assertion_set_sha256
                or requested.observed_at < prerequisite.observed_at
            ):
                _invalid_state()
            existing = load_post_deployment_assertion_observation(store, plan)
            if existing is not None:
                return _result(existing, True)
            db.execute(
                "INSERT INTO post_deployment_assertion_observation "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                requested.database_values(),
            )
        loaded = load_post_deployment_assertion_observation(store, plan)
        if loaded != requested:
            _invalid_state()
        return _result(requested, False)
    except PostDeploymentAssertionObservationError:
        raise
    except (
        AutomationScriptObservationError,
        ResourceAvailabilityError,
        StateError,
        sqlite3.Error,
    ):
        _invalid_state()


def _prerequisite(
    store: StateStore, plan: PostDeploymentAssertionPlan
) -> AutomationScriptObservation:
    try:
        observation = load_automation_script_observation(store, plan.automation_target)
    except AutomationScriptObservationError:
        _invalid_state()
    if observation is None or observation.failed_count != 0:
        raise PostDeploymentAssertionObservationError(
            "Successful automation/script load proof is required"
        )
    return observation


def _canonical(assertions: tuple[PostDeploymentAssertion, ...]) -> str:
    if len(assertions) > MAX_ASSERTIONS:
        _invalid_state()
    identities: set[tuple[str, str]] = set()
    items: list[dict[str, str]] = []
    for assertion in assertions:
        assertion._validate()
        identity = (assertion.kind, assertion.entity_id)
        if identity in identities:
            _invalid_state()
        identities.add(identity)
        items.append({"entity_id": assertion.entity_id, "kind": assertion.kind})
    try:
        encoded = json.dumps(
            {"assertions": items, "version": ASSERTION_SCHEMA_VERSION},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        _invalid_state()
    if len(encoded.encode("ascii")) > MAX_ASSERTION_SET_BYTES:
        _invalid_state()
    return encoded


def _result(
    value: PostDeploymentAssertionObservation, replayed: bool
) -> PostDeploymentAssertionResult:
    status = "passed" if value.failed_count == 0 else "failed"
    return PostDeploymentAssertionResult(
        status,
        replayed,
        value.declared_count,
        value.derived_count,
        value.evaluated_count,
        value.passed_count,
        value.failed_count,
        value.skipped_count,
    )


def _timestamp(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        _invalid_state()
    return current.astimezone(UTC)


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid_state()
    return value


def _invalid_state() -> NoReturn:
    raise PostDeploymentAssertionObservationError(
        "post-deployment assertion observation state is invalid"
    ) from None


def _unavailable() -> NoReturn:
    raise PostDeploymentAssertionObservationError(
        "post-deployment assertion observation is unavailable"
    ) from None
