"""Integrity-bound durable authority for crash-safe rollback plan reconstruction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .post_deployment_assertion_observation import (
    PostDeploymentAssertionObservationError,
    PostDeploymentAssertionPlan,
)
from .prepared_deployment import PreparedDeploymentError, validate_deployment_id

RECOVERY_AUTHORITY_SCHEMA_VERSION = 1
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class RollbackRecoveryAuthorityError(RuntimeError):
    """Recovery authority is malformed, rebound, or integrity-invalid."""


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _canonical_entities(entity_ids: tuple[str, ...]) -> str:
    return json.dumps(entity_ids, ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class RollbackRecoveryAuthority:
    """Non-executable evidence sufficient to deterministically rebuild rollback observation authority."""

    deployment_id: str
    schema_version: int
    candidate_sha: str
    entity_ids_json: str
    resource_target_sha256: str
    automation_target_sha256: str
    assertion_canonical_json: str
    assertion_set_sha256: str
    record_sha256: str

    @classmethod
    def create(cls, plan: PostDeploymentAssertionPlan) -> RollbackRecoveryAuthority:
        """Bind only canonical identities and digests; never serialize executable authority."""
        try:
            plan._validate()
            resource = plan.automation_target.resource_target
            values: tuple[object, ...] = (
                resource.deployment_id,
                RECOVERY_AUTHORITY_SCHEMA_VERSION,
                resource.candidate_sha,
                _canonical_entities(resource.entity_ids),
                resource.target_sha256,
                plan.automation_target.target_sha256,
                plan.canonical_json,
                plan.assertion_set_sha256,
            )
            authority = cls(*values, _digest(values))
            authority.validate()
            return authority
        except RollbackRecoveryAuthorityError:
            raise
        except (AttributeError, PostDeploymentAssertionObservationError, PreparedDeploymentError):
            raise RollbackRecoveryAuthorityError("rollback recovery authority is invalid") from None

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> RollbackRecoveryAuthority:
        if len(row) != 9:
            raise RollbackRecoveryAuthorityError("rollback recovery authority is invalid")
        if not all(isinstance(value, str) for index, value in enumerate(row) if index != 1):
            raise RollbackRecoveryAuthorityError("rollback recovery authority is invalid")
        if type(row[1]) is not int:
            raise RollbackRecoveryAuthorityError("rollback recovery authority is invalid")
        authority = cls(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]
        )
        authority.validate()
        if authority.database_values() != row:
            raise RollbackRecoveryAuthorityError("rollback recovery authority is invalid")
        return authority

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        return (
            self.deployment_id,
            self.schema_version,
            self.candidate_sha,
            self.entity_ids_json,
            self.resource_target_sha256,
            self.automation_target_sha256,
            self.assertion_canonical_json,
            self.assertion_set_sha256,
            self.record_sha256,
        )

    def validate(self) -> None:
        try:
            validate_deployment_id(self.deployment_id)
            entities = json.loads(self.entity_ids_json)
        except (PreparedDeploymentError, json.JSONDecodeError, TypeError):
            raise RollbackRecoveryAuthorityError("rollback recovery authority is invalid") from None
        if (
            self.schema_version != RECOVERY_AUTHORITY_SCHEMA_VERSION
            or _COMMIT.fullmatch(self.candidate_sha) is None
            or not isinstance(entities, list)
            or not all(isinstance(entity, str) for entity in entities)
            or json.dumps(entities, ensure_ascii=True, separators=(",", ":"))
            != self.entity_ids_json
            or any(
                _HASH.fullmatch(value) is None
                for value in (
                    self.resource_target_sha256,
                    self.automation_target_sha256,
                    self.assertion_set_sha256,
                    self.record_sha256,
                )
            )
            or not isinstance(self.assertion_canonical_json, str)
            or _digest(self.database_values_without_digest()) != self.record_sha256
        ):
            raise RollbackRecoveryAuthorityError("rollback recovery authority is invalid")

    def database_values_without_digest(self) -> tuple[object, ...]:
        return (
            self.deployment_id,
            self.schema_version,
            self.candidate_sha,
            self.entity_ids_json,
            self.resource_target_sha256,
            self.automation_target_sha256,
            self.assertion_canonical_json,
            self.assertion_set_sha256,
        )
