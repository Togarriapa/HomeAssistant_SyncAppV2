from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ha_syncapp.candidate_restart_execution import execute_candidate_restart_once
from ha_syncapp.core_restart_transport import (
    CoreRestartResponse,
    load_core_restart_attempt,
)
from test_core_restart_transport import TOKEN, _authorized

NOW = datetime(2026, 9, 27, 23, 0, tzinfo=UTC)


def _claimed(tmp_path, monkeypatch):
    chain, authorization = _authorized(tmp_path, monkeypatch)
    store = chain[0]
    store.enqueue_work("candidate_restart", authorization.deployment_id, now=NOW)
    item = store.claim_work_kind("candidate_restart", now=NOW)
    assert item is not None
    return store, authorization, item


def test_acknowledged_restart_atomically_schedules_observation(tmp_path, monkeypatch) -> None:
    store, authorization, item = _claimed(tmp_path, monkeypatch)
    calls: list[str] = []

    def transport(*_args):
        calls.append("restart")
        assert store._get_work("candidate_restart", item.work_key).status == "running"
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM work WHERE work_kind='candidate_observe' AND work_key=?",
                (item.work_key,),
            ).fetchone()[0]
            == 0
        )
        return CoreRestartResponse(200, "application/json", b'{"result":"ok","data":{}}')

    try:
        result = execute_candidate_restart_once(
            store,
            item,
            token=TOKEN,
            transport=transport,
            now=NOW + timedelta(seconds=1),
        )

        assert calls == ["restart"]
        assert result.action == "observation_scheduled"
        assert result.replayed is False
        assert result.work.status == "succeeded"
        assert result.successor.status == "pending"
        assert load_core_restart_attempt(store, authorization.deployment_id).phase == (
            "request_acknowledged"
        )
    finally:
        store.__exit__(None, None, None)


def test_acknowledged_crash_replay_needs_no_credential_or_network(tmp_path, monkeypatch) -> None:
    store, authorization, item = _claimed(tmp_path, monkeypatch)
    from ha_syncapp.core_restart_transport import request_core_restart_once

    request_core_restart_once(
        store,
        authorization,
        token=TOKEN,
        transport=lambda *_args: CoreRestartResponse(
            200, "application/json", b'{"result":"ok","data":{}}'
        ),
        now=NOW,
    )
    try:
        result = execute_candidate_restart_once(
            store,
            item,
            token=None,
            transport=lambda *_args: (_ for _ in ()).throw(
                AssertionError("acknowledged replay reached network")
            ),
            now=NOW + timedelta(seconds=1),
        )

        assert result.replayed is True
        assert result.work.status == "succeeded"
        assert result.successor.status == "pending"
    finally:
        store.__exit__(None, None, None)


def test_uncertain_restart_is_blocked_without_observation_or_duplicate_post(
    tmp_path, monkeypatch
) -> None:
    store, authorization, item = _claimed(tmp_path, monkeypatch)
    calls = 0

    def uncertain(*_args):
        nonlocal calls
        calls += 1
        raise TimeoutError("secret transport detail")

    try:
        result = execute_candidate_restart_once(
            store,
            item,
            token=TOKEN,
            transport=uncertain,
            now=NOW + timedelta(seconds=1),
        )

        assert calls == 1
        assert result.action == "blocked_uncertain"
        assert result.work.status == "blocked"
        assert result.successor is None
        assert load_core_restart_attempt(store, authorization.deployment_id).phase == (
            "request_started"
        )
    finally:
        store.__exit__(None, None, None)
