from __future__ import annotations

from datetime import timedelta

from ha_syncapp.candidate_apply_execution import execute_candidate_apply_once
from ha_syncapp.live_apply_controller import LiveApplyControllerResult
from ha_syncapp.live_apply_intent_store import record_candidate_apply_admission
from ha_syncapp.post_apply_activation import PostApplyActivationResult
from test_candidate_apply_admission import _prepared_apply
from test_candidate_backup_execution import NOW
from test_live_apply_intent_store import _chain


def _admitted(tmp_path, monkeypatch):
    store, prepared, authority, admission = _prepared_apply(tmp_path, monkeypatch)
    chain = _chain(tmp_path, prepared, monkeypatch)
    record_candidate_apply_admission(
        store,
        admission,
        *chain,
        recorded_at=NOW + timedelta(seconds=7),
    )
    claimed = store.claim_work_kind(
        "candidate_apply_execute", now=NOW + timedelta(seconds=8)
    )
    assert claimed is not None
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_execution.load_candidate_backup_authority",
        lambda *args, **kwargs: authority,
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_execution.reprove_prepared_candidate_backup",
        lambda *args, **kwargs: prepared.evidence,
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_execution.reprove_preapply_repo_heads",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_execution.authorize_candidate_apply",
        lambda *args: chain[0],
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_execution.reprove_stage_for_apply",
        lambda *args: chain[1],
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_execution.build_live_apply_plan",
        lambda *args: chain[2],
    )
    return store, prepared, authority, claimed, chain


def test_one_verified_operation_is_deferred_as_normal_incomplete_work(
    tmp_path, monkeypatch
) -> None:
    store, prepared, _authority, claimed, _chain_value = _admitted(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_execution.advance_live_apply_once",
        lambda *args: calls.append("advance")
        or LiveApplyControllerResult("operation_verified", 0, "a" * 64),
    )
    try:
        result = execute_candidate_apply_once(
            store,
            claimed,
            staging_root=tmp_path,
            home_assistant_root=tmp_path / "homeassistant",
            github_token="github-token",
            supervisor_token="supervisor-token",
            now=NOW + timedelta(seconds=9),
        )

        work = store._get_work("candidate_apply_execute", prepared.deployment_id)
        assert calls == ["advance"]
        assert result.action == "operation_verified"
        assert work.status == "pending"
        assert work.attempts == 0
        assert work.next_attempt_at == NOW + timedelta(seconds=9)
    finally:
        store.__exit__(None, None, None)


def test_complete_apply_authorizes_restart_before_finishing_work(
    tmp_path, monkeypatch
) -> None:
    store, prepared, _authority, claimed, _chain_value = _admitted(tmp_path, monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_execution.advance_live_apply_once",
        lambda *args: LiveApplyControllerResult("complete", None, None, replayed=True),
    )
    monkeypatch.setattr(
        "ha_syncapp.candidate_apply_execution.authorize_post_apply_activation",
        lambda *args, **kwargs: order.append("authorize")
        or PostApplyActivationResult("restart_core", object(), replayed=False),
    )
    original_enqueue = store.enqueue_work

    def enqueue(kind, key, **kwargs):
        order.append("enqueue")
        return original_enqueue(kind, key, **kwargs)

    monkeypatch.setattr(store, "enqueue_work", enqueue)
    try:
        result = execute_candidate_apply_once(
            store,
            claimed,
            staging_root=tmp_path,
            home_assistant_root=tmp_path / "homeassistant",
            github_token="github-token",
            supervisor_token="supervisor-token",
            now=NOW + timedelta(seconds=9),
        )

        assert order == ["authorize", "enqueue"]
        assert result.action == "complete"
        assert store._get_work("candidate_restart", prepared.deployment_id).status == "pending"
        assert store._get_work("candidate_apply_execute", prepared.deployment_id).status == (
            "succeeded"
        )
    finally:
        store.__exit__(None, None, None)
