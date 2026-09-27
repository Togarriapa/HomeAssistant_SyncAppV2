from __future__ import annotations

from datetime import timedelta

from ha_syncapp.candidate_semantic_retrigger import run_candidate_semantic_retrigger_pass
from ha_syncapp.candidate_static_execution import execute_candidate_static_once
from test_candidate_integrity_execution import NOW
from test_candidate_static_execution import _static_ready


def test_retrigger_executes_at_most_one_exact_semantic_action(tmp_path, monkeypatch) -> None:
    store, orchestration = _static_ready(tmp_path, monkeypatch, b"- alias: safe\n")
    try:
        static = execute_candidate_static_once(
            store,
            orchestration,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            now=NOW + timedelta(seconds=3),
        )
        running = store._get_work("candidate", static.orchestration.candidate_sha)
        store.defer_work(running, now=NOW + timedelta(seconds=3))
        calls: list[str] = []

        def execute(_store, current, **kwargs):
            calls.append(current.candidate_sha)
            return type(
                "Result",
                (),
                {
                    "checkpoint": type("Checkpoint", (), {"phase": "completed"})(),
                    "semantic": object(),
                },
            )()

        result = run_candidate_semantic_retrigger_pass(
            store,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            reference_time=NOW + timedelta(seconds=60),
            executor=execute,
        )
        assert result.considered == 1
        assert result.processed == "completed"
        assert calls == [static.orchestration.candidate_sha]
    finally:
        store.__exit__(None, None, None)
