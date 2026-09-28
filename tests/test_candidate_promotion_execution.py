from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp import candidate_promotion_execution as execution
from ha_syncapp.candidate_promotion_execution import (
    CandidatePromotionExecutionError,
    execute_candidate_promotion_once,
)
from ha_syncapp.deployment_promotion import PromotionRemoteState, promote_finalized_deployment_once
from test_core_health_window import START
from test_deployment_promotion import TOKEN, Remote, _ready


def _running(tmp_path, monkeypatch):
    store, plan, baseline, candidate = _ready(tmp_path, monkeypatch)
    monkeypatch.setattr(execution, "_load_plan", lambda *_args: plan)
    now = START + timedelta(seconds=309)
    deployment_id = plan.automation_target.resource_target.deployment_id
    store.enqueue_work("candidate_promote", deployment_id, now=now)
    item = store.claim_work_kind("candidate_promote", now=now)
    assert item is not None
    return store, plan, baseline, candidate, item, now


def test_authorized_promotion_completes_exact_work(tmp_path, monkeypatch) -> None:
    store, _plan, baseline, candidate, item, now = _running(tmp_path, monkeypatch)
    remote = Remote(
        [
            PromotionRemoteState(candidate, baseline, None),
            PromotionRemoteState(candidate, candidate, candidate),
        ]
    )
    try:
        result = execute_candidate_promotion_once(
            store,
            item,
            token=TOKEN,
            remote_reader=remote.read,
            publisher=remote.publish,
            now=now,
        )
        assert result.action == "promotion_completed"
        assert result.status == "completed"
        assert result.work.status == "succeeded"
        assert result.replayed is False
        assert len(remote.publications) == 1
    finally:
        store.__exit__(None, None, None)


def test_completed_promotion_replays_without_credential_or_network(tmp_path, monkeypatch) -> None:
    store, _plan, baseline, candidate, item, now = _running(tmp_path, monkeypatch)
    remote = Remote(
        [
            PromotionRemoteState(candidate, baseline, None),
            PromotionRemoteState(candidate, candidate, candidate),
        ]
    )
    promote_finalized_deployment_once(
        store,
        _plan,
        token=TOKEN,
        remote_reader=remote.read,
        publisher=remote.publish,
        observed_at=now,
    )
    try:
        result = execute_candidate_promotion_once(
            store,
            item,
            token=None,
            remote_reader=lambda *_args: pytest.fail("replay read GitHub"),
            publisher=lambda *_args: pytest.fail("replay published"),
            now=now + timedelta(seconds=1),
        )
        assert result.replayed is True
        assert result.work.status == "succeeded"
    finally:
        store.__exit__(None, None, None)


def test_transport_failure_is_transient_and_leaves_work_running(tmp_path, monkeypatch) -> None:
    store, _plan, _baseline, _candidate, item, now = _running(tmp_path, monkeypatch)
    try:
        with pytest.raises(CandidatePromotionExecutionError) as error:
            execute_candidate_promotion_once(
                store,
                item,
                token=TOKEN,
                remote_reader=lambda *_args: (_ for _ in ()).throw(TimeoutError("secret")),
                publisher=lambda *_args: pytest.fail("unproven refs published"),
                now=now,
            )
        assert error.value.transient is True
        assert store._get_work(item.work_kind, item.work_key).status == "running"
    finally:
        store.__exit__(None, None, None)


def test_ref_divergence_is_deterministic_and_never_publishes(tmp_path, monkeypatch) -> None:
    store, _plan, _baseline, candidate, item, now = _running(tmp_path, monkeypatch)
    remote = Remote([PromotionRemoteState(candidate, "c" * 40, None)])
    try:
        with pytest.raises(CandidatePromotionExecutionError) as error:
            execute_candidate_promotion_once(
                store,
                item,
                token=TOKEN,
                remote_reader=remote.read,
                publisher=remote.publish,
                now=now,
            )
        assert error.value.transient is False
        assert remote.publications == []
    finally:
        store.__exit__(None, None, None)


def test_invalid_work_identity_is_rejected_before_network(tmp_path, monkeypatch) -> None:
    store, _plan, _baseline, _candidate, item, now = _running(tmp_path, monkeypatch)
    wrong = item.__class__(
        "candidate_rollback",
        item.work_key,
        item.status,
        item.attempts,
        item.created_at,
        item.updated_at,
        item.next_attempt_at,
    )
    try:
        with pytest.raises(CandidatePromotionExecutionError) as error:
            execute_candidate_promotion_once(
                store,
                wrong,
                token=TOKEN,
                remote_reader=lambda *_args: pytest.fail("invalid work read GitHub"),
                publisher=lambda *_args: pytest.fail("invalid work published"),
                now=now,
            )
        assert error.value.transient is False
    finally:
        store.__exit__(None, None, None)
