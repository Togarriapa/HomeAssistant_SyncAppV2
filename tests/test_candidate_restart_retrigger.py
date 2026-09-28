from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ha_syncapp.candidate_restart_execution import (
    CandidateRestartExecutionError,
    CandidateRestartExecutionResult,
)
from ha_syncapp.candidate_restart_retrigger import (
    CandidateRestartRetriggerError,
    run_candidate_restart_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2026, 9, 27, 23, 15, tzinfo=UTC)


def _complete(store, item, **kwargs):
    successor = store.enqueue_work("candidate_observe", item.work_key, now=kwargs["now"])
    work = store.complete_work(item, now=kwargs["now"])
    return CandidateRestartExecutionResult(
        item.work_key, "observation_scheduled", False, work, successor
    )


def test_pass_recovers_stale_restart_and_processes_only_one(tmp_path) -> None:
    processed: list[str] = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_restart", "deployment-1", now=NOW)
        store.enqueue_work("candidate_restart", "deployment-2", now=NOW)
        stale = store.claim_work_kind("candidate_restart", now=NOW)
        assert stale is not None

        def execute(owner, item, **kwargs):
            processed.append(item.work_key)
            assert kwargs["token"] is None
            return _complete(owner, item, **kwargs)

        result = run_candidate_restart_retrigger_pass(
            store,
            reference_time=NOW,
            executor=execute,
        )

        assert result.recovered_interrupted == 1
        assert result.considered == 2
        assert result.processed == "observation_scheduled"
        assert len(processed) == 1


def test_executor_failure_blocks_exact_restart_work(tmp_path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_restart", "deployment-1", now=NOW)

        def fail(*args, **kwargs):
            raise CandidateRestartExecutionError("sanitized")

        with pytest.raises(CandidateRestartRetriggerError):
            run_candidate_restart_retrigger_pass(
                store,
                reference_time=NOW,
                executor=fail,
            )

        assert store._get_work("candidate_restart", "deployment-1").status == "blocked"
