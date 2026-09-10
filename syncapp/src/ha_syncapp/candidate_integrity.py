"""Fail-closed integrity gate for exact staged candidate evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import candidate_stage as stage_module
from .candidate_changes import CandidateChange, CandidateChanges
from .candidate_fetch import CandidateFetch
from .candidate_stage import CandidateStage, CandidateStageError

_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODES = {"100644", "100755"}
_STATUSES = {
    "added",
    "deleted",
    "modified",
    "mode_changed",
    "modified_and_mode_changed",
}


class CandidateIntegrityError(RuntimeError):
    """Candidate evidence failed the explicit integrity-validation gate."""


@dataclass(frozen=True, slots=True)
class CandidateIntegrity:
    """Immutable proof that the exact candidate passed integrity validation."""

    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    changed_paths: tuple[str, ...]


def validate_candidate_integrity(
    fetched: CandidateFetch,
    stage: CandidateStage,
    changes: CandidateChanges,
) -> CandidateIntegrity:
    """Validate all immutable candidate evidence without touching live Home Assistant."""
    _validate_bindings(fetched, stage, changes)
    try:
        stage_module.verify_candidate_stage(stage)
        stage_module._reprove_fetch(fetched)
    except CandidateStageError as exc:
        raise CandidateIntegrityError("candidate evidence could not be reverified") from exc

    _validate_changes(changes, stage)

    try:
        stage_module.verify_candidate_stage(stage)
        stage_module._reprove_fetch(fetched)
    except CandidateStageError as exc:
        raise CandidateIntegrityError("candidate evidence changed during integrity validation") from exc

    return CandidateIntegrity(
        target=stage.target,
        repository_id=stage.repository_id,
        baseline_sha=changes.baseline_sha,
        candidate_sha=stage.commit_sha,
        stage_manifest_sha256=stage.manifest_sha256,
        changed_paths=tuple(change.path for change in changes.changes),
    )


def _validate_bindings(
    fetched: CandidateFetch,
    stage: CandidateStage,
    changes: CandidateChanges,
) -> None:
    if (
        type(fetched) is not CandidateFetch
        or type(stage) is not CandidateStage
        or type(changes) is not CandidateChanges
        or fetched.target != stage.target
        or changes.target != stage.target
        or fetched.repository_id != stage.repository_id
        or changes.repository_id != stage.repository_id
        or fetched.branch != "candidate"
        or stage.branch != "candidate"
        or fetched.commit_sha != stage.commit_sha
        or changes.candidate_sha != stage.commit_sha
        or _OBJECT_ID.fullmatch(changes.baseline_sha) is None
        or _OBJECT_ID.fullmatch(changes.candidate_sha) is None
        or _SHA256.fullmatch(stage.manifest_sha256) is None
    ):
        raise CandidateIntegrityError("candidate integrity evidence bindings are invalid")


def _validate_changes(changes: CandidateChanges, stage: CandidateStage) -> None:
    staged = {entry.path: entry for entry in stage.entries}
    previous: bytes | None = None
    seen: set[str] = set()
    for change in changes.changes:
        if type(change) is not CandidateChange:
            raise CandidateIntegrityError("candidate change evidence is invalid")
        _validate_path(change.path)
        encoded = change.path.encode("utf-8")
        if change.path in seen or (previous is not None and encoded <= previous):
            raise CandidateIntegrityError("candidate change evidence is not canonically ordered")
        seen.add(change.path)
        previous = encoded
        _validate_change_shape(change)
        if change.candidate_object_id is not None:
            entry = staged.get(change.path)
            if (
                entry is None
                or entry.git_mode != change.candidate_mode
                or entry.object_id != change.candidate_object_id
            ):
                raise CandidateIntegrityError("candidate change does not match staged evidence")


def _validate_change_shape(change: CandidateChange) -> None:
    if change.status not in _STATUSES:
        raise CandidateIntegrityError("candidate change status is invalid")
    old_present = change.baseline_mode is not None or change.baseline_object_id is not None
    new_present = change.candidate_mode is not None or change.candidate_object_id is not None
    if old_present != (change.baseline_mode is not None and change.baseline_object_id is not None):
        raise CandidateIntegrityError("candidate change baseline shape is invalid")
    if new_present != (change.candidate_mode is not None and change.candidate_object_id is not None):
        raise CandidateIntegrityError("candidate change candidate shape is invalid")
    for mode in (change.baseline_mode, change.candidate_mode):
        if mode is not None and mode not in _MODES:
            raise CandidateIntegrityError("candidate change mode is invalid")
    for object_id in (change.baseline_object_id, change.candidate_object_id):
        if object_id is not None and _OBJECT_ID.fullmatch(object_id) is None:
            raise CandidateIntegrityError("candidate change object ID is invalid")

    content_changed = (
        old_present
        and new_present
        and change.baseline_object_id != change.candidate_object_id
    )
    mode_changed = old_present and new_present and change.baseline_mode != change.candidate_mode
    valid = {
        "added": (not old_present and new_present),
        "deleted": (old_present and not new_present),
        "modified": (old_present and new_present and content_changed and not mode_changed),
        "mode_changed": (old_present and new_present and not content_changed and mode_changed),
        "modified_and_mode_changed": (
            old_present and new_present and content_changed and mode_changed
        ),
    }
    if not valid[change.status]:
        raise CandidateIntegrityError("candidate change status does not match evidence shape")


def _validate_path(path: str) -> None:
    parts = path.split("/") if isinstance(path, str) else []
    if (
        not path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.casefold() == ".git" for part in parts)
    ):
        raise CandidateIntegrityError("candidate change path is unsafe")
