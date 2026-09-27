from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ha_syncapp.live_apply_intent_store import (
    load_live_apply_intent,
    record_candidate_apply_admission,
)
from ha_syncapp.state import StateError, StateStore
from test_live_apply_intent_store import _chain, _prepare_store, _prepared

NOW = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)


def test_apply_intent_and_work_success_are_committed_atomically(tmp_path, monkeypatch) -> None:
    prepared = _prepared()
    chain = _chain(tmp_path, prepared, monkeypatch)
    with StateStore(tmp_path) as store:
        _prepare_store(store, prepared)
        pending = store.enqueue_work("candidate_apply", prepared.deployment_id, now=NOW)
        claimed = store.claim_work_kind("candidate_apply", now=NOW)
        assert claimed is not None and claimed.work_key == pending.work_key

        result = record_candidate_apply_admission(store, claimed, *chain, recorded_at=NOW)

        assert load_live_apply_intent(store, prepared.deployment_id) == result
        completed = store._get_work("candidate_apply", prepared.deployment_id)
        assert completed.status == "succeeded"
        assert completed.next_attempt_at is None


def test_admission_rejects_unclaimed_or_rebound_work_without_persisting(
    tmp_path, monkeypatch
) -> None:
    prepared = _prepared()
    chain = _chain(tmp_path, prepared, monkeypatch)
    with StateStore(tmp_path) as store:
        _prepare_store(store, prepared)
        pending = store.enqueue_work("candidate_apply", prepared.deployment_id, now=NOW)

        with pytest.raises(StateError, match="claimed Apply work"):
            record_candidate_apply_admission(store, pending, *chain, recorded_at=NOW)

        assert load_live_apply_intent(store, prepared.deployment_id) is None
        assert store._get_work("candidate_apply", prepared.deployment_id).status == "pending"


def test_admission_rejects_work_for_another_deployment(tmp_path, monkeypatch) -> None:
    prepared = _prepared()
    chain = _chain(tmp_path, prepared, monkeypatch)
    with StateStore(tmp_path) as store:
        _prepare_store(store, prepared)
        store.enqueue_work("candidate_apply", "0" * 36, now=NOW)
        claimed = store.claim_work_kind("candidate_apply", now=NOW)
        assert claimed is not None

        with pytest.raises(StateError, match="binding"):
            record_candidate_apply_admission(store, claimed, *chain, recorded_at=NOW)

        assert load_live_apply_intent(store, prepared.deployment_id) is None
        assert store._get_work("candidate_apply", claimed.work_key).status == "running"


def test_work_transition_failure_rolls_back_new_apply_intent(tmp_path, monkeypatch) -> None:
    prepared = _prepared()
    chain = _chain(tmp_path, prepared, monkeypatch)
    with StateStore(tmp_path) as store:
        _prepare_store(store, prepared)
        store.enqueue_work("candidate_apply", prepared.deployment_id, now=NOW)
        claimed = store.claim_work_kind("candidate_apply", now=NOW)
        assert claimed is not None
        store._connection.execute(
            "CREATE TRIGGER reject_apply_completion BEFORE UPDATE OF status ON work "
            "WHEN NEW.status = 'succeeded' BEGIN SELECT RAISE(ABORT, 'rejected'); END"
        )

        with pytest.raises(StateError, match="Unable to persist candidate Apply admission"):
            record_candidate_apply_admission(store, claimed, *chain, recorded_at=NOW)

        assert load_live_apply_intent(store, prepared.deployment_id) is None
        assert store._get_work("candidate_apply", prepared.deployment_id).status == "running"
