from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_finalization_execution import (
    CandidateFinalizationExecutionError,
    CandidateFinalizationExecutionResult,
)
from ha_syncapp.candidate_finalization_retrigger import (
    CandidateFinalizationRetriggerError,
    run_candidate_finalization_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2031, 2, 3, 4, 19, tzinfo=UTC)


def _complete(store, item, **kwargs):
    successor = store.enqueue_work("candidate_promote", item.work_key, now=kwargs["now"])
    work = store.complete_work(item, now=kwargs["now"])
    return CandidateFinalizationExecutionResult(
        item.work_key,
        "promotion_scheduled",
        "success",
        "promote_and_tag",
        "none",
        False,
        False,
        5,
        work,
        successor,
    )


def test_pass_recovers_stale_work_and_processes_only_one(tmp_path) -> None:
    processed: list[str] = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_finalize", "deployment-1", now=NOW)
        store.enqueue_work("candidate_finalize", "deployment-2", now=NOW)
        stale = store.claim_work_kind("candidate_finalize", now=NOW)
        assert stale is not None

        def execute(owner, item, **kwargs):
            processed.append(item.work_key)
            return _complete(owner, item, **kwargs)

        result = run_candidate_finalization_retrigger_pass(
            store, reference_time=NOW, executor=execute
        )
        assert result.recovered_interrupted == 1
        assert result.considered == 2
        assert result.processed == "promotion_scheduled"
        assert len(processed) == 1


@pytest.mark.parametrize(
    ("transient", "expected_status"),
    [(True, "retry"), (False, "blocked")],
)
def test_failure_classification_controls_retry(
    tmp_path, transient: bool, expected_status: str
) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_finalize", "deployment-1", now=NOW)

        def fail(*_args, **_kwargs):
            raise CandidateFinalizationExecutionError("sanitized", transient=transient)

        with pytest.raises(CandidateFinalizationRetriggerError) as caught:
            run_candidate_finalization_retrigger_pass(store, reference_time=NOW, executor=fail)
        assert caught.value.transient is transient
        work = store._get_work("candidate_finalize", "deployment-1")
        assert work.status == expected_status
        if transient:
            assert work.next_attempt_at == NOW + timedelta(seconds=60)


def test_invalid_executor_result_blocks_work(tmp_path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_finalize", "deployment-1", now=NOW)

        def invalid(owner, item, **kwargs):
            result = _complete(owner, item, **kwargs)
            return CandidateFinalizationExecutionResult(
                result.deployment_id,
                "rollback_scheduled",
                result.outcome,
                result.authority,
                result.failure_stage,
                result.candidate_blocked,
                result.replayed,
                result.completed_predicate_count,
                result.work,
                result.successor,
            )

        with pytest.raises(CandidateFinalizationRetriggerError) as caught:
            run_candidate_finalization_retrigger_pass(store, reference_time=NOW, executor=invalid)
        assert caught.value.transient is False
