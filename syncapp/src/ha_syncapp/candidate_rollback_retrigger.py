"""Bounded Retrigger lane for candidate rollback authorization."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .candidate_rollback_execution import (
    CandidateRollbackExecutionError,
    CandidateRollbackExecutionResult,
    execute_candidate_rollback_once,
)
from .deploy_key_rollback_authority import DeployKeyRollbackRepositoryAuthority
from .deployment_rollback import BackupReader, RepositoryReader
from .deployment_rollback_transport import (
    read_rollback_backup_proof,
    read_rollback_repository_proof,
)
from .state import StateError, StateStore, WorkItem


class CandidateRollbackRetriggerError(RuntimeError):
    """Candidate rollback authorization recovery failed closed."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidateRollbackRetriggerResult:
    """Bounded, identity-free outcome of one rollback handoff pass."""

    recovered_interrupted: int
    considered: int
    processed: str | None


Executor = Callable[..., CandidateRollbackExecutionResult]
_VALID_PHASES = frozenset(
    {
        "planned",
        "restore_started",
        "restore_acknowledged",
        "uncertain",
        "observing",
        "completed",
        "blocked",
    }
)


def run_candidate_rollback_retrigger_pass(
    store: StateStore,
    github_token: str | None,
    supervisor_token: str | None,
    *,
    repository_reader: RepositoryReader | None = None,
    repository_authority: DeployKeyRollbackRepositoryAuthority | None = None,
    backup_reader: BackupReader = read_rollback_backup_proof,
    reference_time: datetime | None = None,
    executor: Executor = execute_candidate_rollback_once,
) -> CandidateRollbackRetriggerResult:
    """Recover interrupted authorization work and process at most one item."""
    when = datetime.now(UTC) if reference_time is None else reference_time
    claimed: WorkItem | None = None
    if (
        type(store) is not StateStore
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _error(False)
    try:
        current = when.astimezone(UTC).isoformat()
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            recovered = db.execute(
                "UPDATE work SET status='retry',updated_at=?,next_attempt_at=? "
                "WHERE work_kind='candidate_rollback' AND status='running'",
                (current, current),
            ).rowcount
            rows = db.execute(
                "SELECT work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at FROM work WHERE work_kind='candidate_rollback' "
                "AND status IN ('pending','retry') AND next_attempt_at<=? "
                "ORDER BY next_attempt_at,created_at,work_key LIMIT 2",
                (current,),
            ).fetchall()
            if not rows:
                return CandidateRollbackRetriggerResult(recovered, 0, None)
            item = store._work_from_row(rows[0])
            changed = db.execute(
                "UPDATE work SET status='running',attempts=attempts+1,updated_at=?,"
                "next_attempt_at=NULL WHERE work_kind=? AND work_key=? "
                "AND status=? AND attempts=?",
                (current, item.work_kind, item.work_key, item.status, item.attempts),
            )
            if changed.rowcount != 1:
                _error(False)
        claimed = store._get_work(item.work_kind, item.work_key)
        selected_reader = repository_reader
        if repository_authority is None and selected_reader is None:
            selected_reader = read_rollback_repository_proof
        if repository_authority is None:
            result = executor(
                store,
                claimed,
                github_token=github_token,
                supervisor_token=supervisor_token,
                repository_reader=selected_reader,
                backup_reader=backup_reader,
                now=when,
            )
        else:
            result = executor(
                store,
                claimed,
                github_token=github_token,
                supervisor_token=supervisor_token,
                repository_reader=selected_reader,
                repository_authority=repository_authority,
                backup_reader=backup_reader,
                now=when,
            )
        current_work = store._get_work(claimed.work_kind, claimed.work_key)
        if (
            result.action != "rollback_authorized"
            or result.status not in _VALID_PHASES
            or result.deployment_id != claimed.work_key
            or result.work != current_work
            or current_work.status != "succeeded"
        ):
            _error(False)
        return CandidateRollbackRetriggerResult(recovered, len(rows), result.action)
    except CandidateRollbackExecutionError as error:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=error.transient, now=when)
        _error(error.transient)
    except CandidateRollbackRetriggerError:
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        raise
    except (StateError, sqlite3.Error):
        if claimed is not None:
            with suppress(StateError):
                store.fail_work(claimed, transient=False, now=when)
        _error(False)


def _error(transient: bool) -> NoReturn:
    raise CandidateRollbackRetriggerError(
        "candidate rollback authorization failed closed", transient=transient
    ) from None
