"""Bounded Retrigger lane for admitted candidate live Apply execution."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .candidate_apply_execution import (
    CandidateApplyExecutionError,
    CandidateApplyExecutionResult,
    execute_candidate_apply_once,
)
from .state import StateError, StateStore, WorkItem


class CandidateApplyExecutionRetriggerError(RuntimeError):
    """Candidate Apply execution recovery failed closed."""


@dataclass(frozen=True, slots=True)
class CandidateApplyExecutionRetriggerResult:
    """Bounded, identity-free outcome of one Apply execution recovery pass."""

    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateApplyExecutionResult]


def run_candidate_apply_execution_retrigger_pass(
    store: StateStore,
    *,
    staging_root: Path,
    home_assistant_root: Path,
    github_token: str | None,
    supervisor_token: str | None,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_apply_once,
) -> CandidateApplyExecutionRetriggerResult:
    """Recover interrupted execution and advance at most one eligible candidate."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if (
        type(store) is not StateStore
        or not isinstance(staging_root, Path)
        or not staging_root.is_absolute()
        or not isinstance(home_assistant_root, Path)
        or not home_assistant_root.is_absolute()
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        raise CandidateApplyExecutionRetriggerError(
            "candidate Apply execution Retrigger inputs are invalid"
        )
    try:
        current = when.astimezone(UTC).isoformat()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            recovered = db.execute(
                "UPDATE work SET status='retry',updated_at=?,next_attempt_at=? "
                "WHERE work_kind='candidate_apply_execute' AND status='running'",
                (current, current),
            ).rowcount
            rows = db.execute(
                "SELECT work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at FROM work WHERE work_kind='candidate_apply_execute' "
                "AND status IN ('pending','retry') AND next_attempt_at<=? "
                "ORDER BY next_attempt_at,created_at,work_key LIMIT 2",
                (current,),
            ).fetchall()
            if not rows:
                return CandidateApplyExecutionRetriggerResult(recovered, 0, None)
            item = store._work_from_row(rows[0])
            changed = db.execute(
                "UPDATE work SET status='running',attempts=attempts+1,updated_at=?,"
                "next_attempt_at=NULL WHERE work_kind=? AND work_key=? "
                "AND status=? AND attempts=?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if changed.rowcount != 1:
                raise CandidateApplyExecutionRetriggerError(
                    "candidate Apply execution claim changed"
                )
        claimed = store._get_work(item.work_kind, item.work_key)
        result = executor(
            store,
            claimed,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            github_token=github_token,
            supervisor_token=supervisor_token,
            now=when,
        )
        current_work = store._get_work(claimed.work_kind, claimed.work_key)
        expected = {
            "operation_verified": "pending",
            "reconciled": "pending",
            "blocked": "blocked",
            "complete": "succeeded",
        }.get(result.action)
        if (
            result.deployment_id != claimed.work_key
            or expected is None
            or current_work != result.work
            or current_work.status != expected
        ):
            raise CandidateApplyExecutionRetriggerError(
                "candidate Apply execution completion is invalid"
            )
        return CandidateApplyExecutionRetriggerResult(recovered, len(rows), result.action)
    except CandidateApplyExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        raise CandidateApplyExecutionRetriggerError(
            "candidate Apply execution failed closed"
        ) from None
    except (CandidateApplyExecutionRetriggerError, StateError, sqlite3.Error):
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise CandidateApplyExecutionRetriggerError(
            "candidate Apply execution failed closed"
        ) from None
