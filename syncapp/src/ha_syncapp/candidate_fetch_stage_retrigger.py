"""Bounded Retrigger lane for one exact candidate Fetch/Stage action."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .candidate_fetch_stage_execution import (
    CandidateFetchStageExecutionError,
    CandidateFetchStageResult,
    execute_candidate_fetch_stage_once,
)
from .candidate_orchestration import (
    CandidateOrchestrationError,
    load_candidate_orchestration,
    register_claimed_candidate,
)
from .state import StateError, StateStore, WorkItem

_WORK_KIND = "candidate"


class CandidateFetchStageRetriggerError(RuntimeError):
    """Candidate Fetch/Stage recovery could not safely complete."""


@dataclass(frozen=True, slots=True)
class CandidateFetchStageRetriggerResult:
    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateFetchStageResult]


def run_candidate_fetch_stage_retrigger_pass(
    store: StateStore,
    home_assistant_root: Path,
    workspace_root: Path,
    staging_root: Path,
    target: str,
    github_token: str,
    *,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_fetch_stage_once,
) -> CandidateFetchStageRetriggerResult:
    """Recover candidate work and execute at most one eligible Fetch/Stage action."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if type(store) is not StateStore or when.tzinfo is None or when.utcoffset() is None:
        raise CandidateFetchStageRetriggerError("candidate Fetch/Stage inputs are invalid")
    try:
        _prepare_private_root(workspace_root, home_assistant_root)
        _prepare_private_root(staging_root, home_assistant_root)
        recovered = _recover_interrupted_candidate_work(store, when)
        considered, claimed = _claim_eligible_candidate(store, when)
        if claimed is None:
            return CandidateFetchStageRetriggerResult(recovered, considered, None)
        orchestration = load_candidate_orchestration(store, claimed.work_key)
        if orchestration is None:
            repository_id = store.repository_id(target)
            if repository_id is None:
                store.fail_work(claimed, transient=False, now=when)
                raise CandidateFetchStageRetriggerError(
                    "candidate Fetch/Stage repository authority is unavailable"
                )
            orchestration = register_claimed_candidate(
                store,
                claimed,
                target=target,
                repository_id=repository_id,
                now=when,
            )
        result = executor(
            store,
            orchestration,
            token=github_token,
            workspace_root=workspace_root,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            now=when,
        )
        store.defer_work(claimed, now=when)
        return CandidateFetchStageRetriggerResult(recovered, considered, result.checkpoint.phase)
    except CandidateFetchStageExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        raise CandidateFetchStageRetriggerError("candidate Fetch/Stage failed closed") from None
    except CandidateOrchestrationError:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise CandidateFetchStageRetriggerError("candidate Fetch/Stage failed closed") from None
    except (StateError, sqlite3.Error, OSError):
        raise CandidateFetchStageRetriggerError("candidate Fetch/Stage failed closed") from None


def _recover_interrupted_candidate_work(store: StateStore, when: datetime) -> int:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "UPDATE work SET status = 'retry', updated_at = ?, next_attempt_at = ? "
                "WHERE work_kind = ? AND status = 'running'",
                (when.astimezone(UTC).isoformat(), when.astimezone(UTC).isoformat(), _WORK_KIND),
            )
        return result.rowcount
    except sqlite3.Error:
        raise CandidateFetchStageRetriggerError(
            "candidate Fetch/Stage recovery is unavailable"
        ) from None


def _claim_eligible_candidate(store: StateStore, when: datetime) -> tuple[int, WorkItem | None]:
    current = when.astimezone(UTC).isoformat()
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT w.work_kind, w.work_key, w.status, w.attempts, w.created_at, "
                "w.updated_at, w.next_attempt_at FROM work AS w "
                "LEFT JOIN candidate_orchestration AS c ON c.candidate_sha = w.work_key "
                "WHERE w.work_kind = ? AND w.status IN ('pending','retry') "
                "AND w.next_attempt_at <= ? AND (c.candidate_sha IS NULL "
                "OR c.next_action = 'fetch_stage') ORDER BY w.next_attempt_at, "
                "w.created_at, w.work_key LIMIT 2",
                (_WORK_KIND, current),
            ).fetchall()
            considered = len(rows)
            if not rows:
                return 0, None
            item = store._work_from_row(rows[0])
            result = db.execute(
                "UPDATE work SET status = 'running', attempts = attempts + 1, "
                "updated_at = ?, next_attempt_at = NULL WHERE work_kind = ? "
                "AND work_key = ? AND status = ? AND attempts = ?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if result.rowcount != 1:
                raise CandidateFetchStageRetriggerError(
                    "candidate Fetch/Stage claim changed unexpectedly"
                )
        return considered, store._get_work(item.work_kind, item.work_key)
    except sqlite3.Error:
        raise CandidateFetchStageRetriggerError(
            "candidate Fetch/Stage claim is unavailable"
        ) from None


def _prepare_private_root(path: Path, home_assistant_root: Path) -> None:
    if not isinstance(path, Path) or not isinstance(home_assistant_root, Path):
        raise CandidateFetchStageRetriggerError("candidate Fetch/Stage roots are invalid")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
            raise CandidateFetchStageRetriggerError("candidate Fetch/Stage roots are unsafe")
        os.chmod(path, 0o700, follow_symlinks=False)
        metadata = path.lstat()
        root = path.resolve(strict=True)
        home = home_assistant_root.resolve(strict=True)
    except CandidateFetchStageRetriggerError:
        raise
    except OSError:
        raise CandidateFetchStageRetriggerError(
            "candidate Fetch/Stage roots are unavailable"
        ) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or root == home
        or root in home.parents
        or home in root.parents
    ):
        raise CandidateFetchStageRetriggerError("candidate Fetch/Stage roots are unsafe")
