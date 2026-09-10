"""Immutable preparation records; persistence does not authorize live Apply."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from .candidate_backup import CandidateBackupEvidence

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_HASH = re.compile(r"[0-9a-f]{64}")
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
_CORE = re.compile(r"20[0-9]{2}\.(?:[1-9]|1[0-2])\.(?:0|[1-9][0-9]*)")
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class PreparedDeploymentError(RuntimeError):
    """Preparation evidence is malformed or has lost its integrity binding."""


def validate_deployment_id(value: object) -> None:
    if not isinstance(value, str):
        raise PreparedDeploymentError("Invalid prepared deployment identity")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise PreparedDeploymentError("Invalid prepared deployment identity") from None


@dataclass(frozen=True, slots=True)
class PreparedDeployment:
    """One exact candidate and verified backup association, never an Apply permit."""

    deployment_id: str
    evidence: CandidateBackupEvidence
    prepared_at: datetime

    def validate(self) -> None:
        # Import at use time: backup gates depend on the StateStore through staging.
        from .candidate_backup import CandidateBackupEvidence

        validate_deployment_id(self.deployment_id)
        evidence = self.evidence
        if type(evidence) is not CandidateBackupEvidence:
            raise PreparedDeploymentError("Invalid prepared deployment evidence")
        strings = (
            (evidence.target, _TARGET),
            (evidence.baseline_sha, _COMMIT),
            (evidence.candidate_sha, _COMMIT),
            (evidence.stage_manifest_sha256, _HASH),
            (evidence.runtime_sha256, _HASH),
            (evidence.core_version, _CORE),
            (evidence.backup_slug, _SLUG),
        )
        if (
            any(
                not isinstance(value, str) or pattern.fullmatch(value) is None
                for value, pattern in strings
            )
            or type(evidence.repository_id) is not int
            or not 0 < evidence.repository_id <= 2**63 - 1
            or not isinstance(evidence.risk_level, str)
            or evidence.risk_level not in {"low", "medium", "high", "critical"}
            or evidence.baseline_sha == evidence.candidate_sha
            or len(evidence.baseline_sha) != len(evidence.candidate_sha)
        ):
            raise PreparedDeploymentError("Invalid prepared deployment evidence")
        if (
            not isinstance(self.prepared_at, datetime)
            or self.prepared_at.tzinfo is None
            or self.prepared_at.utcoffset() is None
        ):
            raise PreparedDeploymentError("Invalid prepared deployment timestamp")

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values = (
            self.deployment_id,
            *astuple(self.evidence),
            self.prepared_at.astimezone(UTC).isoformat(),
        )
        digest = hashlib.sha256(
            json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        return (*values, digest)

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> PreparedDeployment:
        from .candidate_backup import CandidateBackupEvidence

        if len(row) != 12 or type(row[2]) is not int:
            raise PreparedDeploymentError("Invalid prepared deployment record")

        def text(index: int) -> str:
            value = row[index]
            if not isinstance(value, str):
                raise PreparedDeploymentError("Invalid prepared deployment record")
            return value

        try:
            when = datetime.fromisoformat(text(10))
        except ValueError:
            raise PreparedDeploymentError("Invalid prepared deployment timestamp") from None
        record = cls(
            deployment_id=text(0),
            evidence=CandidateBackupEvidence(
                target=text(1),
                repository_id=row[2],
                baseline_sha=text(3),
                candidate_sha=text(4),
                stage_manifest_sha256=text(5),
                runtime_sha256=text(6),
                risk_level=text(7),
                core_version=text(8),
                backup_slug=text(9),
            ),
            prepared_at=when,
        )
        # Compare all values as well as the digest, rejecting noncanonical timestamps.
        if record.database_values() != row:
            raise PreparedDeploymentError("Prepared deployment integrity check failed")
        return record
