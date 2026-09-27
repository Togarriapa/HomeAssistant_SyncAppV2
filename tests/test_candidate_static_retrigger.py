from __future__ import annotations

from datetime import timedelta

from ha_syncapp.candidate_risk_execution import execute_candidate_risk_once
from ha_syncapp.candidate_static_retrigger import run_candidate_static_retrigger_pass
from test_candidate_integrity_execution import NOW
from test_candidate_risk_execution import _ready_for_risk


def test_retrigger_executes_at_most_one_exact_static_action(tmp_path, monkeypatch) -> None:
    store, dependency = _ready_for_risk(tmp_path)
    try:
        risk = execute_candidate_risk_once(
            store, dependency.orchestration, now=NOW + timedelta(seconds=2)
        )
        running = store._get_work("candidate", risk.orchestration.candidate_sha)
        store.defer_work(running, now=NOW + timedelta(seconds=2))
        calls: list[str] = []

        def execute(_store, orchestration, **kwargs):
            calls.append(orchestration.candidate_sha)
            return type(
                "Result",
                (),
                {
                    "checkpoint": type("Checkpoint", (), {"phase": "completed"})(),
                    "validation": type("Validation", (), {"syntax_valid": True})(),
                },
            )()

        result = run_candidate_static_retrigger_pass(
            store,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            reference_time=NOW + timedelta(seconds=60),
            executor=execute,
        )
        assert result.considered == 1
        assert result.processed == "completed"
        assert calls == [risk.orchestration.candidate_sha]
    finally:
        store.__exit__(None, None, None)
