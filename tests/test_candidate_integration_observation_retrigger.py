from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_integration_observation_execution import (
    CandidateIntegrationObservationExecutionError,
    CandidateIntegrationObservationExecutionResult,
)
from ha_syncapp.candidate_integration_observation_retrigger import (
    CandidateIntegrationObservationRetriggerError,
    run_candidate_integration_observation_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2031, 2, 3, 4, 13, tzinfo=UTC)


def _complete(store, item, **kwargs):
    successor = store.enqueue_work(
        "candidate_observe_startup_errors", item.work_key, now=kwargs["now"]
    )
    work = store.complete_work(item, now=kwargs["now"])
    return CandidateIntegrationObservationExecutionResult(
        item.work_key,
        "startup_error_observation_scheduled",
        False,
        2,
        1,
        work,
        successor,
    )


def test_pass_recovers_stale_work_and_processes_only_one(tmp_path) -> None:
    processed: list[str] = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_observe_integrations", "deployment-1", now=NOW)
        store.enqueue_work("candidate_observe_integrations", "deployment-2", now=NOW)
        stale = store.claim_work_kind("candidate_observe_integrations", now=NOW)
        assert stale is not None

        def execute(owner, item, **kwargs):
            processed.append(item.work_key)
            assert kwargs["token"] is None
            return _complete(owner, item, **kwargs)

        result = run_candidate_integration_observation_retrigger_pass(
            store,
            reference_time=NOW,
            executor=execute,
        )
        assert result.recovered_interrupted == 1
        assert result.considered == 2
        assert result.processed == "startup_error_observation_scheduled"
        assert len(processed) == 1


@pytest.mark.parametrize(
    ("transient", "expected_status"),
    [(True, "retry"), (False, "blocked")],
)
def test_failure_classification_controls_retry(tmp_path, transient, expected_status) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_observe_integrations", "deployment-1", now=NOW)

        def fail(*_args, **_kwargs):
            raise CandidateIntegrationObservationExecutionError("sanitized", transient=transient)

        with pytest.raises(CandidateIntegrationObservationRetriggerError) as caught:
            run_candidate_integration_observation_retrigger_pass(
                store,
                reference_time=NOW,
                executor=fail,
            )
        assert caught.value.transient is transient
        work = store._get_work("candidate_observe_integrations", "deployment-1")
        assert work.status == expected_status
        if transient:
            assert work.next_attempt_at == NOW + timedelta(seconds=60)
