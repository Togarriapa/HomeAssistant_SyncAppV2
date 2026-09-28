from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_assertion_observation_execution import (
    CandidateAssertionObservationExecutionError,
    CandidateAssertionObservationExecutionResult,
)
from ha_syncapp.candidate_assertion_observation_retrigger import (
    CandidateAssertionObservationRetriggerError,
    run_candidate_assertion_observation_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2031, 2, 3, 4, 18, tzinfo=UTC)


def _complete(store, item, **kwargs):
    successor = store.enqueue_work("candidate_finalize", item.work_key, now=kwargs["now"])
    work = store.complete_work(item, now=kwargs["now"])
    return CandidateAssertionObservationExecutionResult(
        item.work_key,
        "finalization_scheduled",
        "passed",
        False,
        0,
        2,
        2,
        2,
        0,
        0,
        work,
        successor,
    )


def test_pass_recovers_stale_work_and_processes_only_one(tmp_path) -> None:
    processed: list[str] = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_observe_assertions", "deployment-1", now=NOW)
        store.enqueue_work("candidate_observe_assertions", "deployment-2", now=NOW)
        stale = store.claim_work_kind("candidate_observe_assertions", now=NOW)
        assert stale is not None

        def execute(owner, item, **kwargs):
            processed.append(item.work_key)
            assert kwargs["token"] is None
            return _complete(owner, item, **kwargs)

        result = run_candidate_assertion_observation_retrigger_pass(
            store, reference_time=NOW, executor=execute
        )
        assert result.recovered_interrupted == 1
        assert result.considered == 2
        assert result.processed == "finalization_scheduled"
        assert len(processed) == 1


@pytest.mark.parametrize(
    ("transient", "expected_status"),
    [(True, "retry"), (False, "blocked")],
)
def test_failure_classification_controls_retry(
    tmp_path, transient: bool, expected_status: str
) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_observe_assertions", "deployment-1", now=NOW)

        def fail(*_args, **_kwargs):
            raise CandidateAssertionObservationExecutionError("sanitized", transient=transient)

        with pytest.raises(CandidateAssertionObservationRetriggerError) as caught:
            run_candidate_assertion_observation_retrigger_pass(
                store, reference_time=NOW, executor=fail
            )
        assert caught.value.transient is transient
        work = store._get_work("candidate_observe_assertions", "deployment-1")
        assert work.status == expected_status
        if transient:
            assert work.next_attempt_at == NOW + timedelta(seconds=60)
