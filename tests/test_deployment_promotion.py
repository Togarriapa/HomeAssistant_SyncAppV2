from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp.deployment_finalization import finalize_deployment_once
from ha_syncapp.deployment_promotion import (
    DeploymentPromotionError,
    PromotionRemoteState,
    load_deployment_promotion,
    promote_finalized_deployment_once,
)
from test_core_health_window import START
from test_deployment_finalization import _successful

TOKEN = "github-secret-sentinel"


class Remote:
    def __init__(self, states: list[PromotionRemoteState]):
        self.states = states
        self.reads = 0
        self.publications: list[tuple[str, PromotionRemoteState]] = []

    def read(self, target, token, repository_id, tag):
        assert target == "Owner/Home"
        assert token == TOKEN
        assert repository_id == 42
        assert tag.startswith("syncapp-known-good-")
        value = self.states[min(self.reads, len(self.states) - 1)]
        self.reads += 1
        return value

    def publish(self, intent, state, token):
        assert token == TOKEN
        self.publications.append((intent.record_sha256, state))


def _ready(tmp_path, monkeypatch):
    chain, plan = _successful(tmp_path, monkeypatch)
    store = chain[0]
    finalize_deployment_once(
        store, plan, finalized_at=START + timedelta(seconds=308)
    )
    prepared = store.prepared_deployment(
        plan.automation_target.resource_target.deployment_id
    )
    assert prepared is not None
    baseline = prepared.evidence.baseline_sha
    candidate = prepared.evidence.candidate_sha
    return store, plan, baseline, candidate


def test_exact_success_is_published_completed_and_replayed_without_network(
    tmp_path, monkeypatch
):
    store, plan, baseline, candidate = _ready(tmp_path, monkeypatch)
    remote = Remote(
        [
            PromotionRemoteState(candidate, baseline, None),
            PromotionRemoteState(candidate, candidate, candidate),
        ]
    )
    try:
        first = promote_finalized_deployment_once(
            store,
            plan,
            token=TOKEN,
            remote_reader=remote.read,
            publisher=remote.publish,
            observed_at=START + timedelta(seconds=309),
        )
        replay = promote_finalized_deployment_once(
            store,
            plan,
            token=None,
            remote_reader=lambda *_args: pytest.fail("completed replay read network"),
            publisher=lambda *_args: pytest.fail("completed replay published"),
        )
        assert first.status == replay.status == "completed"
        assert first.replayed is False
        assert replay.replayed is True
        assert remote.reads == 2
        assert len(remote.publications) == 1
    finally:
        store.__exit__(None, None, None)


def test_crash_after_atomic_publication_is_reconciled_without_second_push(
    tmp_path, monkeypatch
):
    store, plan, baseline, candidate = _ready(tmp_path, monkeypatch)
    published = PromotionRemoteState(candidate, candidate, candidate)
    remote = Remote([PromotionRemoteState(candidate, baseline, None), published])

    def crash_after_publish(intent, state, token):
        remote.publish(intent, state, token)
        remote.states = [published]
        raise OSError("secret crash detail")

    try:
        with pytest.raises(DeploymentPromotionError, match="unavailable") as error:
            promote_finalized_deployment_once(
                store,
                plan,
                token=TOKEN,
                remote_reader=remote.read,
                publisher=crash_after_publish,
                observed_at=START + timedelta(seconds=309),
            )
        assert "secret crash detail" not in str(error.value)
        planned = load_deployment_promotion(store, plan)
        assert planned is not None and planned.phase == "planned"

        recovered = promote_finalized_deployment_once(
            store,
            plan,
            token=TOKEN,
            remote_reader=remote.read,
            publisher=lambda *_args: pytest.fail(
                "reconciliation repeated publication"
            ),
            observed_at=START + timedelta(seconds=310),
        )
        assert recovered.status == "completed"
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "state",
    [
        PromotionRemoteState("c" * 40, "a" * 40, None),
        PromotionRemoteState("b" * 40, "c" * 40, None),
        PromotionRemoteState("b" * 40, "a" * 40, "c" * 40),
    ],
)
def test_candidate_main_or_tag_divergence_is_blocked_before_publication(
    tmp_path, monkeypatch, state
):
    store, plan, _baseline, _candidate = _ready(tmp_path, monkeypatch)
    remote = Remote([state])
    try:
        with pytest.raises(DeploymentPromotionError, match="blocked"):
            promote_finalized_deployment_once(
                store,
                plan,
                token=TOKEN,
                remote_reader=remote.read,
                publisher=remote.publish,
                observed_at=START + timedelta(seconds=309),
            )
        assert remote.publications == []
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "state",
    [
        lambda baseline, candidate: PromotionRemoteState(candidate, candidate, None),
        lambda baseline, candidate: PromotionRemoteState(candidate, baseline, candidate),
    ],
)
def test_safe_partial_remote_state_completes_only_missing_ref(
    tmp_path, monkeypatch, state
):
    store, plan, baseline, candidate = _ready(tmp_path, monkeypatch)
    initial = state(baseline, candidate)
    complete = PromotionRemoteState(candidate, candidate, candidate)
    remote = Remote([initial, complete])
    try:
        result = promote_finalized_deployment_once(
            store,
            plan,
            token=TOKEN,
            remote_reader=remote.read,
            publisher=remote.publish,
            observed_at=START + timedelta(seconds=309),
        )
        assert result.status == "completed"
        promotion = load_deployment_promotion(store, plan)
        assert promotion is not None
        assert remote.publications == [(promotion.record_sha256, initial)]
    finally:
        store.__exit__(None, None, None)


def test_persistence_failure_prevents_network_and_schema_22_migrates(
    tmp_path, monkeypatch
):
    store, plan, baseline, candidate = _ready(tmp_path, monkeypatch)
    root = store._root
    store._connection.execute(
        "CREATE TRIGGER reject_promotion BEFORE INSERT ON deployment_promotion "
        "BEGIN SELECT RAISE(ABORT, 'secret-storage-detail'); END"
    )
    remote = Remote([PromotionRemoteState(candidate, baseline, None)])
    try:
        with pytest.raises(DeploymentPromotionError, match="state is invalid") as error:
            promote_finalized_deployment_once(
                store,
                plan,
                token=TOKEN,
                remote_reader=remote.read,
                publisher=remote.publish,
                observed_at=START + timedelta(seconds=309),
            )
        assert "secret-storage-detail" not in str(error.value)
        assert remote.reads == 0
        store._connection.execute("DROP TRIGGER reject_promotion")
        store._connection.execute("DROP TABLE deployment_promotion")
        store._connection.execute("PRAGMA user_version = 22")
        store._connection.commit()
    finally:
        store.__exit__(None, None, None)

    from ha_syncapp.state import StateStore

    with StateStore(root) as reopened:
        assert reopened._connection.execute("PRAGMA user_version").fetchone()[0] == 23
        assert reopened._connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'deployment_promotion'"
        ).fetchone() == ("deployment_promotion",)
