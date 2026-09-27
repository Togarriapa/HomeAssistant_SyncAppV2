"""Bounded, non-mutating admission of one prepared candidate to live Apply."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from .apply_authorization import ApplyAuthorizationError, authorize_candidate_apply
from .candidate_backup import (
    CandidateBackupError,
    reprove_prepared_candidate_backup,
)
from .candidate_backup_execution import (
    CandidateBackupExecutionError,
    load_candidate_backup_authority,
)
from .candidate_integrity_execution import load_candidate_integrity_checkpoint
from .candidate_orchestration import load_candidate_orchestration
from .live_apply_intent_store import (
    PersistedLiveApplyIntent,
    record_candidate_apply_admission,
)
from .live_apply_plan import LiveApplyPlanError, build_live_apply_plan
from .live_apply_preconditions import (
    LiveApplyPreconditionError,
    prove_live_apply_preconditions,
)
from .preapply_freshness import PreApplyFreshnessError, reprove_preapply_repo_heads
from .stage_prewrite_reproof import StagePrewriteReproofError, reprove_stage_for_apply
from .state import StateError, StateStore, WorkItem


class CandidateApplyAdmissionError(RuntimeError):
    """A prepared candidate could not be admitted safely to live Apply."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateApplyAdmissionResult:
    """Content-minimal result of one completed Apply admission action."""

    deployment_id: str
    intent: PersistedLiveApplyIntent


def execute_candidate_apply_admission_once(
    store: StateStore,
    item: WorkItem,
    *,
    staging_root: Path,
    home_assistant_root: Path,
    github_token: str | None,
    supervisor_token: str | None,
    now: datetime | None = None,
) -> CandidateApplyAdmissionResult:
    """Re-prove every pre-Apply boundary and persist intent without live mutation."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_apply"
        or item.status != "running"
        or item.attempts < 1
        or not isinstance(staging_root, Path)
        or not staging_root.is_absolute()
        or not isinstance(home_assistant_root, Path)
        or not home_assistant_root.is_absolute()
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _reject(False)
    try:
        prepared = store.prepared_deployment(item.work_key)
        if prepared is None:
            _reject(False)
        current = load_candidate_orchestration(store, prepared.evidence.candidate_sha)
        if (
            current is None
            or current.phase != "completed"
            or current.next_action != "none"
            or current.target != prepared.evidence.target
            or current.repository_id != prepared.evidence.repository_id
        ):
            _reject(False)
        authority = load_candidate_backup_authority(
            store,
            current,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
        )
        integrity = load_candidate_integrity_checkpoint(store, current.candidate_sha)
        if integrity is None or integrity.phase != "completed":
            _reject(False)
        backup = reprove_prepared_candidate_backup(
            prepared,
            authority.semantic,
            authority.static,
            authority.integrity,
            authority.stage,
            authority.dependencies,
            authority.impact,
            authority.risk,
            authority.runtime,
            authority.version,
            token=supervisor_token,
        )
        freshness = reprove_preapply_repo_heads(
            prepared,
            backup,
            token=github_token,
        )
        authorization = authorize_candidate_apply(prepared, backup, freshness)
        stage_evidence = reprove_stage_for_apply(authorization, authority.stage)
        plan = build_live_apply_plan(stage_evidence, authority.stage, integrity.changes())
        preconditions = prove_live_apply_preconditions(plan, home_assistant_root)
        intent = record_candidate_apply_admission(
            store,
            item,
            authorization,
            stage_evidence,
            plan,
            preconditions,
            recorded_at=when,
        )
        return CandidateApplyAdmissionResult(prepared.deployment_id, intent)
    except CandidateApplyAdmissionError:
        raise
    except CandidateBackupExecutionError as error:
        _reject(error.transient)
    except (CandidateBackupError, PreApplyFreshnessError):
        _reject(True)
    except (
        ApplyAuthorizationError,
        StagePrewriteReproofError,
        LiveApplyPlanError,
        LiveApplyPreconditionError,
        StateError,
    ):
        _reject(False)
    except Exception:
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidateApplyAdmissionError(
        "Candidate Apply admission failed closed",
        transient=transient,
    ) from None
