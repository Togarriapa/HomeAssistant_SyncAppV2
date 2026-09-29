"""Authorized execution of one finalized candidate promotion handoff."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from .deploy_key_promotion_authority import DeployKeyPromotionAuthority

from .candidate_assertion_observation_execution import (
    CandidateAssertionObservationExecutionError,
    _load_plan,
)
from .deployment_promotion import (
    DeploymentPromotionError,
    Publisher,
    RemoteReader,
    load_deployment_promotion,
    promote_finalized_deployment_once,
)
from .post_deployment_assertion_observation import PostDeploymentAssertionPlan
from .state import StateError, StateStore, WorkItem


class CandidatePromotionExecutionError(RuntimeError):
    """Candidate promotion work could not advance safely."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class CandidatePromotionExecutionResult:
    """Identity-free result of an authorized promotion pass."""

    deployment_id: str
    action: str
    status: str
    replayed: bool
    work: WorkItem


def execute_candidate_promotion_once(
    store: StateStore,
    item: WorkItem,
    *,
    token: str | None,
    remote_reader: RemoteReader | None = None,
    publisher: Publisher | None = None,
    promotion_authority: DeployKeyPromotionAuthority | None = None,
    now: datetime | None = None,
) -> CandidatePromotionExecutionResult:
    """Re-prove authority, reconcile Git refs, then complete exact work."""
    when = datetime.now(UTC) if now is None else now
    if (
        type(store) is not StateStore
        or type(item) is not WorkItem
        or item.work_kind != "candidate_promote"
        or item.status != "running"
        or item.attempts < 1
        or not isinstance(when, datetime)
        or when.tzinfo is None
        or when.utcoffset() is None
    ):
        _reject(False)
    when = when.astimezone(UTC)
    try:
        plan = _load_plan(store, item.work_key)
        promoted = promote_finalized_deployment_once(
            store,
            plan,
            token=token,
            remote_reader=remote_reader,
            publisher=publisher,
            promotion_authority=promotion_authority,
            observed_at=when,
        )
        if promoted.status != "completed":
            _reject(False)
        work = _finish(store, item, when, plan)
        return CandidatePromotionExecutionResult(
            item.work_key,
            "promotion_completed",
            promoted.status,
            promoted.replayed,
            work,
        )
    except CandidatePromotionExecutionError:
        raise
    except DeploymentPromotionError as error:
        _reject(error.transient)
    except (CandidateAssertionObservationExecutionError, StateError, sqlite3.Error):
        _reject(False)


def _finish(
    store: StateStore,
    item: WorkItem,
    when: datetime,
    plan: PostDeploymentAssertionPlan,
) -> WorkItem:
    try:
        with store._connection as db:
            db.execute("BEGIN IMMEDIATE")
            current_plan = _load_plan(store, item.work_key)
            promotion = load_deployment_promotion(store, current_plan)
            if current_plan != plan or promotion is None or promotion.phase != "completed":
                _reject(False)
            changed = db.execute(
                "UPDATE work SET status='succeeded',updated_at=?,next_attempt_at=NULL "
                "WHERE work_kind='candidate_promote' AND work_key=? "
                "AND status='running' AND attempts=?",
                (when.isoformat(), item.work_key, item.attempts),
            )
            if changed.rowcount != 1:
                _reject(False)
        work = store._get_work(item.work_kind, item.work_key)
        if work.status != "succeeded":
            _reject(False)
        return work
    except CandidatePromotionExecutionError:
        raise
    except DeploymentPromotionError as error:
        _reject(error.transient)
    except (CandidateAssertionObservationExecutionError, StateError, sqlite3.Error):
        _reject(False)


def _reject(transient: bool) -> NoReturn:
    raise CandidatePromotionExecutionError(
        "Candidate promotion failed closed", transient=transient
    ) from None
