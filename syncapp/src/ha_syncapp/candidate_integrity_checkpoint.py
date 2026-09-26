"""Durable, canonical evidence for candidate change and integrity analysis."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_changes import CandidateChange, CandidateChanges

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_SCHEMA_VERSION = 1
_MAX_CHANGES = 4096
_MAX_CHANGES_BYTES = 4 * 1024 * 1024
_STATUSES = {
    "added",
    "deleted",
    "modified",
    "mode_changed",
    "modified_and_mode_changed",
}
_MODES = {"100644", "100755"}


class CandidateIntegrityCheckpointError(RuntimeError):
    """Persisted candidate analysis evidence is missing, corrupt or rebound."""


@dataclass(frozen=True, slots=True)
class CandidateIntegrityCheckpoint:
    """Integrity-protected checkpoint for one exact staged candidate."""

    candidate_sha: str
    schema_version: int
    orchestration_sha256: str
    fetch_stage_sha256: str
    target: str
    repository_id: int
    stage_manifest_sha256: str
    phase: str
    baseline_sha: str | None
    changes_json: str | None
    changed_count: int | None
    planned_at: datetime
    completed_at: datetime | None
    record_sha256: str

    @classmethod
    def plan(
        cls,
        *,
        candidate_sha: str,
        orchestration_sha256: str,
        fetch_stage_sha256: str,
        target: str,
        repository_id: int,
        stage_manifest_sha256: str,
        planned_at: datetime,
    ) -> CandidateIntegrityCheckpoint:
        when = _timestamp(planned_at)
        result = cls(
            candidate_sha,
            _SCHEMA_VERSION,
            orchestration_sha256,
            fetch_stage_sha256,
            target,
            repository_id,
            stage_manifest_sha256,
            "planned",
            None,
            None,
            None,
            when,
            None,
            "0" * 64,
        )
        planned = replace(result, record_sha256=_digest(result.values_without_digest()))
        planned.validate()
        return planned

    def complete(
        self,
        changes: CandidateChanges,
        *,
        completed_at: datetime,
    ) -> CandidateIntegrityCheckpoint:
        self.validate()
        if self.phase != "planned":
            _invalid()
        _validate_changes(changes)
        if (
            changes.target != self.target
            or changes.repository_id != self.repository_id
            or changes.candidate_sha != self.candidate_sha
        ):
            _invalid()
        when = _timestamp(completed_at)
        payload = _serialize_changes(changes.changes)
        result = replace(
            self,
            phase="completed",
            baseline_sha=changes.baseline_sha,
            changes_json=payload,
            changed_count=len(changes.changes),
            completed_at=when,
            record_sha256="0" * 64,
        )
        completed = replace(result, record_sha256=_digest(result.values_without_digest()))
        completed.validate()
        return completed

    @classmethod
    def from_database_row(cls, row: tuple[object, ...]) -> CandidateIntegrityCheckpoint:
        if len(row) != 14 or type(row[5]) is not int:
            _invalid()
        try:
            planned_at = datetime.fromisoformat(_text(row[11]))
            completed_at = None if row[12] is None else datetime.fromisoformat(_text(row[12]))
        except ValueError:
            _invalid()
        result = cls(
            _text(row[0]),
            _integer(row[1]),
            _text(row[2]),
            _text(row[3]),
            _text(row[4]),
            _integer(row[5]),
            _text(row[6]),
            _text(row[7]),
            None if row[8] is None else _text(row[8]),
            None if row[9] is None else _text(row[9]),
            None if row[10] is None else _integer(row[10]),
            planned_at,
            completed_at,
            _text(row[13]),
        )
        result.validate()
        if result.database_values() != row:
            _invalid()
        return result

    def values_without_digest(self) -> tuple[object, ...]:
        return (
            self.candidate_sha,
            self.schema_version,
            self.orchestration_sha256,
            self.fetch_stage_sha256,
            self.target,
            self.repository_id,
            self.stage_manifest_sha256,
            self.phase,
            self.baseline_sha,
            self.changes_json,
            self.changed_count,
            self.planned_at.astimezone(UTC).isoformat(),
            None if self.completed_at is None else self.completed_at.astimezone(UTC).isoformat(),
        )

    def database_values(self) -> tuple[object, ...]:
        self.validate()
        values = self.values_without_digest()
        return (*values, _digest(values))

    def changes(self) -> CandidateChanges:
        self.validate()
        if self.phase != "completed" or self.changes_json is None or self.baseline_sha is None:
            _invalid()
        parsed = _parse_changes(self.changes_json)
        result = CandidateChanges(
            target=self.target,
            repository_id=self.repository_id,
            baseline_sha=self.baseline_sha,
            candidate_sha=self.candidate_sha,
            changes=parsed,
        )
        _validate_changes(result)
        if len(parsed) != self.changed_count:
            _invalid()
        return result

    def validate(self) -> None:
        completed = self.phase == "completed"
        if (
            _COMMIT.fullmatch(self.candidate_sha) is None
            or self.schema_version != _SCHEMA_VERSION
            or _HASH.fullmatch(self.orchestration_sha256) is None
            or _HASH.fullmatch(self.fetch_stage_sha256) is None
            or _TARGET.fullmatch(self.target) is None
            or type(self.repository_id) is not int
            or self.repository_id <= 0
            or _HASH.fullmatch(self.stage_manifest_sha256) is None
            or self.phase not in {"planned", "completed"}
            or completed != (self.baseline_sha is not None)
            or completed != (self.changes_json is not None)
            or completed != (self.changed_count is not None)
            or completed != (self.completed_at is not None)
            or (self.baseline_sha is not None and _OBJECT_ID.fullmatch(self.baseline_sha) is None)
            or (self.changed_count is not None and not 0 <= self.changed_count <= _MAX_CHANGES)
            or self.planned_at.tzinfo is None
            or self.planned_at.utcoffset() is None
            or (self.completed_at is not None and self.completed_at < self.planned_at)
            or _HASH.fullmatch(self.record_sha256) is None
        ):
            _invalid()
        if self.changes_json is not None:
            try:
                encoded_changes = self.changes_json.encode("ascii", errors="strict")
            except UnicodeEncodeError:
                _invalid()
            if len(encoded_changes) > _MAX_CHANGES_BYTES:
                _invalid()
            parsed = _parse_changes(self.changes_json)
            if _serialize_changes(parsed) != self.changes_json or len(parsed) != self.changed_count:
                _invalid()
        if self.record_sha256 != _digest(self.values_without_digest()):
            _invalid()


def _serialize_changes(changes: tuple[CandidateChange, ...]) -> str:
    if len(changes) > _MAX_CHANGES:
        _invalid()
    payload = [
        {
            "baseline_mode": change.baseline_mode,
            "baseline_object_id": change.baseline_object_id,
            "candidate_mode": change.candidate_mode,
            "candidate_object_id": change.candidate_object_id,
            "path": change.path,
            "status": change.status,
        }
        for change in changes
    ]
    result = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(result.encode("ascii")) > _MAX_CHANGES_BYTES:
        _invalid()
    return result


def _parse_changes(payload: str) -> tuple[CandidateChange, ...]:
    try:
        raw = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeEncodeError, ValueError, TypeError):
        _invalid()
    if not isinstance(raw, list) or len(raw) > _MAX_CHANGES:
        _invalid()
    changes: list[CandidateChange] = []
    expected = {
        "baseline_mode",
        "baseline_object_id",
        "candidate_mode",
        "candidate_object_id",
        "path",
        "status",
    }
    for item in raw:
        if not isinstance(item, dict) or set(item) != expected:
            _invalid()
        optional = (
            item["baseline_mode"],
            item["baseline_object_id"],
            item["candidate_mode"],
            item["candidate_object_id"],
        )
        if (
            not isinstance(item["path"], str)
            or not isinstance(item["status"], str)
            or any(value is not None and not isinstance(value, str) for value in optional)
        ):
            _invalid()
        changes.append(
            CandidateChange(
                path=item["path"],
                status=item["status"],
                baseline_mode=item["baseline_mode"],
                baseline_object_id=item["baseline_object_id"],
                candidate_mode=item["candidate_mode"],
                candidate_object_id=item["candidate_object_id"],
            )
        )
    return tuple(changes)


def _validate_changes(result: CandidateChanges) -> None:
    if (
        type(result) is not CandidateChanges
        or _TARGET.fullmatch(result.target) is None
        or type(result.repository_id) is not int
        or result.repository_id <= 0
        or _OBJECT_ID.fullmatch(result.baseline_sha) is None
        or _COMMIT.fullmatch(result.candidate_sha) is None
        or type(result.changes) is not tuple
        or len(result.changes) > _MAX_CHANGES
    ):
        _invalid()
    previous: bytes | None = None
    for change in result.changes:
        if type(change) is not CandidateChange:
            _invalid()
        parts = change.path.split("/") if isinstance(change.path, str) else []
        encoded = change.path.encode("utf-8") if parts else b""
        if (
            not parts
            or change.path.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or any(part.casefold() == ".git" for part in parts)
            or change.status not in _STATUSES
            or (previous is not None and encoded <= previous)
        ):
            _invalid()
        previous = encoded
        old = change.baseline_mode is not None or change.baseline_object_id is not None
        new = change.candidate_mode is not None or change.candidate_object_id is not None
        if old != (change.baseline_mode is not None and change.baseline_object_id is not None):
            _invalid()
        if new != (change.candidate_mode is not None and change.candidate_object_id is not None):
            _invalid()
        for mode in (change.baseline_mode, change.candidate_mode):
            if mode is not None and mode not in _MODES:
                _invalid()
        for object_id in (change.baseline_object_id, change.candidate_object_id):
            if object_id is not None and _OBJECT_ID.fullmatch(object_id) is None:
                _invalid()
        content = old and new and change.baseline_object_id != change.candidate_object_id
        mode_changed = old and new and change.baseline_mode != change.candidate_mode
        valid = {
            "added": not old and new,
            "deleted": old and not new,
            "modified": old and new and content and not mode_changed,
            "mode_changed": old and new and not content and mode_changed,
            "modified_and_mode_changed": old and new and content and mode_changed,
        }
        if not valid[change.status]:
            _invalid()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid()
    return value.astimezone(UTC)


def _digest(values: tuple[object, ...]) -> str:
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _text(value: object) -> str:
    if not isinstance(value, str):
        _invalid()
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        _invalid()
    return value


def _invalid() -> NoReturn:
    raise CandidateIntegrityCheckpointError("Candidate integrity checkpoint is invalid")
