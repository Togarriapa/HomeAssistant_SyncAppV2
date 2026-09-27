"""Bounded Retrigger lane for one exact candidate backup action."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .candidate_backup_execution import (
    CandidateBackupExecutionError,
    CandidateBackupExecutionResult,
    execute_candidate_backup_once,
)
from .candidate_orchestration import CandidateOrchestrationError, load_candidate_orchestration
from .state import StateError, StateStore, WorkItem


class CandidateBackupRetriggerError(RuntimeError):
    """Candidate backup recovery failed closed."""


@dataclass(frozen=True, slots=True)
class CandidateBackupRetriggerResult:
    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateBackupExecutionResult]


def run_candidate_backup_retrigger_pass(
    store: StateStore,
    *,
    staging_root: Path,
    home_assistant_root: Path,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_backup_once,
) -> CandidateBackupRetriggerResult:
    """Recover interrupted work and execute at most one eligible backup action."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if (
        type(store) is not StateStore
        or when.tzinfo is None
        or when.utcoffset() is None
        or not staging_root.is_absolute()
        or not home_assistant_root.is_absolute()
    ):
        raise CandidateBackupRetriggerError("candidate backup inputs are invalid")
    try:
        current = when.astimezone(UTC).isoformat()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            recovered = db.execute(
                "UPDATE work SET status='retry',updated_at=?,next_attempt_at=? "
                "WHERE work_kind='candidate' AND status='running' AND work_key IN "
                "(SELECT candidate_sha FROM candidate_orchestration "
                "WHERE next_action='prepare_backup')",
                (current, current),
            ).rowcount
            rows = db.execute(
                "SELECT w.work_kind,w.work_key,w.status,w.attempts,w.created_at,w.updated_at,"
                "w.next_attempt_at FROM work w JOIN candidate_orchestration c "
                "ON c.candidate_sha=w.work_key WHERE w.work_kind='candidate' "
                "AND w.status IN ('pending','retry') AND w.next_attempt_at<=? "
                "AND c.next_action='prepare_backup' "
                "ORDER BY w.next_attempt_at,w.created_at,w.work_key LIMIT 2",
                (current,),
            ).fetchall()
            if not rows:
                return CandidateBackupRetriggerResult(recovered, 0, None)
            item = store._work_from_row(rows[0])
            changed = db.execute(
                "UPDATE work SET status='running',attempts=attempts+1,updated_at=?,"
                "next_attempt_at=NULL WHERE work_kind=? AND work_key=? AND status=? AND attempts=?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if changed.rowcount != 1:
                raise CandidateBackupRetriggerError("candidate backup claim changed")
        claimed = store._get_work(item.work_kind, item.work_key)
        orchestration = load_candidate_orchestration(store, claimed.work_key)
        if orchestration is None:
            raise CandidateBackupRetriggerError("candidate backup authority is unavailable")
        result = executor(
            store,
            orchestration,
            staging_root=staging_root,
            home_assistant_root=home_assistant_root,
            now=when,
        )
        return CandidateBackupRetriggerResult(recovered, len(rows), result.checkpoint.phase)
    except CandidateBackupExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        raise CandidateBackupRetriggerError("candidate backup failed closed") from None
    except (
        CandidateBackupRetriggerError,
        CandidateOrchestrationError,
        StateError,
        sqlite3.Error,
    ):
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise CandidateBackupRetriggerError("candidate backup failed closed") from None
