"""Bounded execution of one authorized Home Assistant Core restart."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from .core_restart_transport import (
    CoreRestartError,
    CoreRestartTransport,
    load_core_restart_attempt,
    request_core_restart_once,
)
from .post_apply_activation import (
    PostApplyActivationError,
    load_post_apply_activation_authorization,
)
from .state import StateError, StateStore, WorkItem


class CandidateRestartExecutionError(RuntimeError):
    """An authorized candidate restart could not be advanced safely."""


@dataclass(frozen=True, slots=True)
class CandidateRestartExecutionResult:
    """Content-minimal outcome of one bounded restart action."""

    deployment_id: str
    action: str
    replayed: bool
    work: WorkItem
    successor: WorkItem | None


def execute_candidate_restart_once(
    store: StateStore,
    item: WorkItem,
    *,
    token: str | None = None,
    transport: CoreRestartTransport | None = None,
    now: datetime | None = None,
) -> CandidateRestartExecutionResult:
    """Issue at most one journaled restart and schedule observation after acknowledgement."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_restart"
        or item.status != "running"
        or item.attempts < 1
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _reject()
    when = when.astimezone(UTC)
    try:
        authorization = load_post_apply_activation_authorization(store, item.work_key)
        if authorization is None or authorization.deployment_id != item.work_key:
            _reject()
        try:
            restart = request_core_restart_once(
                store,
                authorization,
                token=token,
                transport=transport,
                now=when,
            )
        except CoreRestartError:
            return _block(store, item, when)
        if restart.status == "reconciliation_required":
            return _block(store, item, when)
        if restart.status != "request_acknowledged":
            _reject()
        work, successor = _finish_acknowledged(store, item, when)
        return CandidateRestartExecutionResult(
            item.work_key,
            "observation_scheduled",
            restart.replayed,
            work,
            successor,
        )
    except CandidateRestartExecutionError:
        raise
    except (CoreRestartError, PostApplyActivationError, StateError, sqlite3.Error):
        _reject()


def _block(store: StateStore, item: WorkItem, when: datetime) -> CandidateRestartExecutionResult:
    try:
        attempt = load_core_restart_attempt(store, item.work_key)
    except CoreRestartError:
        attempt = None
    work = store.fail_work(item, transient=False, now=when)
    action = (
        "blocked_uncertain"
        if attempt is not None and attempt.phase == "request_started"
        else "blocked_invalid"
    )
    return CandidateRestartExecutionResult(item.work_key, action, False, work, None)


def _finish_acknowledged(
    store: StateStore, item: WorkItem, when: datetime
) -> tuple[WorkItem, WorkItem]:
    current = when.isoformat()
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            authorization = load_post_apply_activation_authorization(store, item.work_key)
            attempt = load_core_restart_attempt(store, item.work_key)
            if (
                authorization is None
                or attempt is None
                or attempt.phase != "request_acknowledged"
                or attempt.authorization_record_sha256 != authorization.record_sha256
            ):
                _reject()
            existing = db.execute(
                "SELECT 1 FROM work WHERE work_kind='candidate_observe' AND work_key=?",
                (item.work_key,),
            ).fetchone()
            if existing is not None:
                _reject()
            db.execute(
                "INSERT INTO work (work_kind,work_key,status,attempts,created_at,updated_at,"
                "next_attempt_at) VALUES ('candidate_observe',?,'pending',0,?,?,?)",
                (item.work_key, current, current, current),
            )
            changed = db.execute(
                "UPDATE work SET status='succeeded',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate_restart' AND work_key=? AND status='running' "
                "AND attempts=?",
                (current, item.work_key, item.attempts),
            )
            if changed.rowcount != 1:
                _reject()
        work = store._get_work(item.work_kind, item.work_key)
        successor = store._get_work("candidate_observe", item.work_key)
        if work is None or successor is None:
            _reject()
        if work.status != "succeeded" or successor.status != "pending":
            _reject()
        return work, successor
    except CandidateRestartExecutionError:
        raise
    except (CoreRestartError, PostApplyActivationError, StateError, sqlite3.Error):
        _reject()


def _reject() -> NoReturn:
    raise CandidateRestartExecutionError("Candidate restart execution failed closed") from None
