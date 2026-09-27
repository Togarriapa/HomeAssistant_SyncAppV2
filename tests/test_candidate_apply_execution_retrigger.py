from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ha_syncapp.candidate_apply_execution import (
    CandidateApplyExecutionError,
    CandidateApplyExecutionResult,
)
from ha_syncapp.candidate_apply_execution_retrigger import (
    CandidateApplyExecutionRetriggerError,
    run_candidate_apply_execution_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2026, 9, 27, 22, 0, tzinfo=UTC)


def _defer(store, item, **kwargs):
    work = store.defer_work(item, now=kwargs["now"])
    return CandidateApplyExecutionResult(item.work_key, "operation_verified", 0, False, work)


def test_pass_recovers_stale_work_and_advances_only_one_action(tmp_path) -> None:
    processed: list[str] = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_apply_execute", "deployment-1", now=NOW)
        store.enqueue_work("candidate_apply_execute", "deployment-2", now=NOW)
        stale = store.claim_work_kind("candidate_apply_execute", now=NOW)
        assert stale is not None

        def execute(owner, item, **kwargs):
            processed.append(item.work_key)
            return _defer(owner, item, **kwargs)

        result = run_candidate_apply_execution_retrigger_pass(
            store,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            github_token="github-token",
            supervisor_token="supervisor-token",
            reference_time=NOW,
            executor=execute,
        )

        assert result.recovered_interrupted == 1
        assert result.considered == 2
        assert result.processed == "operation_verified"
        assert len(processed) == 1


@pytest.mark.parametrize(
    ("transient", "expected_status"),
    [(True, "retry"), (False, "blocked")],
)
def test_failure_policy_controls_retry_or_durable_block(
    tmp_path, transient, expected_status
) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_apply_execute", "deployment-1", now=NOW)

        def fail(*args, **kwargs):
            raise CandidateApplyExecutionError("sanitized", transient=transient)

        with pytest.raises(CandidateApplyExecutionRetriggerError):
            run_candidate_apply_execution_retrigger_pass(
                store,
                staging_root=tmp_path,
                home_assistant_root=tmp_path,
                github_token="github-token",
                supervisor_token="supervisor-token",
                reference_time=NOW,
                executor=fail,
            )

        failed = store._get_work("candidate_apply_execute", "deployment-1")
        assert failed.status == expected_status
        assert (failed.next_attempt_at is not None) is transient
