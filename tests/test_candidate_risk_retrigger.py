from __future__ import annotations

from datetime import timedelta

from ha_syncapp.candidate_risk_retrigger import run_candidate_risk_retrigger_pass
from test_candidate_integrity_execution import NOW
from test_candidate_risk_execution import _ready_for_risk


def test_retrigger_executes_at_most_one_exact_risk_action(tmp_path) -> None:
    store, dependency = _ready_for_risk(tmp_path)
    try:
        running = store._get_work("candidate", dependency.orchestration.candidate_sha)
        store.defer_work(running, now=NOW)
        result = run_candidate_risk_retrigger_pass(
            store, reference_time=NOW + timedelta(seconds=60)
        )
        assert result.considered == 1
        assert result.processed == "completed"
        replay = run_candidate_risk_retrigger_pass(
            store, reference_time=NOW + timedelta(seconds=120)
        )
        assert replay.processed is None
    finally:
        store.__exit__(None, None, None)
