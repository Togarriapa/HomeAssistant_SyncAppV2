from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_core_observation_execution import (
    CandidateCoreObservationExecutionError,
    CandidateCoreObservationExecutionResult,
)
from ha_syncapp.candidate_core_observation_retrigger import (
    CandidateCoreObservationRetriggerError,
    run_candidate_core_observation_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC)


def _defer(store, item, **kwargs):
    work = store.defer_work(item, now=kwargs["now"])
    return CandidateCoreObservationExecutionResult(
        item.work_key, "initial_health_recorded", False, work, None, None
    )


def test_pass_recovers_stale_work_and_processes_only_one(tmp_path) -> None:
    processed: list[str] = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_observe", "deployment-1", now=NOW)
        store.enqueue_work("candidate_observe", "deployment-2", now=NOW)
        stale = store.claim_work_kind("candidate_observe", now=NOW)
        assert stale is not None

        def execute(owner, item, **kwargs):
            processed.append(item.work_key)
            assert kwargs["observation_seconds"] == 300
            assert kwargs["token"] is None
            return _defer(owner, item, **kwargs)

        result = run_candidate_core_observation_retrigger_pass(
            store,
            observation_seconds=300,
            reference_time=NOW,
            executor=execute,
        )

        assert result.recovered_interrupted == 1
        assert result.considered == 2
        assert result.processed == "initial_health_recorded"
        assert len(processed) == 1


@pytest.mark.parametrize(
    ("transient", "expected_status"),
    [(True, "retry"), (False, "blocked")],
)
def test_failure_classification_controls_retry(tmp_path, transient, expected_status) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_observe", "deployment-1", now=NOW)

        def fail(*_args, **_kwargs):
            raise CandidateCoreObservationExecutionError("sanitized", transient=transient)

        with pytest.raises(CandidateCoreObservationRetriggerError) as caught:
            run_candidate_core_observation_retrigger_pass(
                store,
                observation_seconds=300,
                reference_time=NOW,
                executor=fail,
            )
        assert caught.value.transient is transient
        work = store._get_work("candidate_observe", "deployment-1")
        assert work.status == expected_status
        if transient:
            assert work.next_attempt_at == NOW + timedelta(seconds=60)
