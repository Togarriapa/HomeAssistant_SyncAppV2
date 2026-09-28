from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_startup_error_observation_execution import (
    CandidateStartupErrorObservationExecutionError,
    CandidateStartupErrorObservationExecutionResult,
)
from ha_syncapp.candidate_startup_error_observation_retrigger import (
    CandidateStartupErrorObservationRetriggerError,
    run_candidate_startup_error_observation_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2031, 2, 3, 4, 14, tzinfo=UTC)


def _complete(store: StateStore, item: object, **kwargs: object):
    successor = store.enqueue_work("candidate_observe_resources", item.work_key, now=kwargs["now"])
    work = store.complete_work(item, now=kwargs["now"])
    return CandidateStartupErrorObservationExecutionResult(
        item.work_key,
        "resource_observation_scheduled",
        "clear",
        False,
        0,
        0,
        0,
        work,
        successor,
    )


def test_pass_recovers_stale_work_and_processes_only_one(tmp_path) -> None:
    processed: list[str] = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_observe_startup_errors", "deployment-1", now=NOW)
        store.enqueue_work("candidate_observe_startup_errors", "deployment-2", now=NOW)
        stale = store.claim_work_kind("candidate_observe_startup_errors", now=NOW)
        assert stale is not None

        def execute(owner, item, **kwargs):
            processed.append(item.work_key)
            assert kwargs["token"] is None
            return _complete(owner, item, **kwargs)

        result = run_candidate_startup_error_observation_retrigger_pass(
            store, reference_time=NOW, executor=execute
        )

        assert result.recovered_interrupted == 1
        assert result.considered == 2
        assert result.processed == "resource_observation_scheduled"
        assert len(processed) == 1


@pytest.mark.parametrize(
    ("transient", "expected_status"),
    [(True, "retry"), (False, "blocked")],
)
def test_failure_classification_controls_retry(
    tmp_path, transient: bool, expected_status: str
) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_observe_startup_errors", "deployment-1", now=NOW)

        def fail(*_args, **_kwargs):
            raise CandidateStartupErrorObservationExecutionError("sanitized", transient=transient)

        with pytest.raises(CandidateStartupErrorObservationRetriggerError) as caught:
            run_candidate_startup_error_observation_retrigger_pass(
                store, reference_time=NOW, executor=fail
            )

        assert caught.value.transient is transient
        work = store._get_work("candidate_observe_startup_errors", "deployment-1")
        assert work.status == expected_status
        if transient:
            assert work.next_attempt_at == NOW + timedelta(seconds=60)
