from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_promotion_execution import (
    CandidatePromotionExecutionError,
    CandidatePromotionExecutionResult,
)
from ha_syncapp.candidate_promotion_retrigger import (
    CandidatePromotionRetriggerError,
    run_candidate_promotion_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2031, 2, 3, 4, 20, tzinfo=UTC)
TOKEN = "github-secret-sentinel"


def _complete(store, item, **kwargs):
    work = store.complete_work(item, now=kwargs["now"])
    return CandidatePromotionExecutionResult(
        item.work_key,
        "promotion_completed",
        "completed",
        False,
        work,
    )


def test_pass_recovers_stale_work_and_processes_only_one(tmp_path) -> None:
    processed: list[str] = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_promote", "deployment-1", now=NOW)
        store.enqueue_work("candidate_promote", "deployment-2", now=NOW)
        stale = store.claim_work_kind("candidate_promote", now=NOW)
        assert stale is not None

        def execute(owner, item, **kwargs):
            assert kwargs["token"] == TOKEN
            processed.append(item.work_key)
            return _complete(owner, item, **kwargs)

        result = run_candidate_promotion_retrigger_pass(
            store, TOKEN, reference_time=NOW, executor=execute
        )
        assert result.recovered_interrupted == 1
        assert result.considered == 2
        assert result.processed == "promotion_completed"
        assert len(processed) == 1


@pytest.mark.parametrize(
    ("transient", "expected_status"),
    [(True, "retry"), (False, "blocked")],
)
def test_failure_classification_controls_retry(
    tmp_path, transient: bool, expected_status: str
) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_promote", "deployment-1", now=NOW)

        def fail(*_args, **_kwargs):
            raise CandidatePromotionExecutionError("sanitized", transient=transient)

        with pytest.raises(CandidatePromotionRetriggerError) as caught:
            run_candidate_promotion_retrigger_pass(store, TOKEN, reference_time=NOW, executor=fail)
        assert caught.value.transient is transient
        work = store._get_work("candidate_promote", "deployment-1")
        assert work.status == expected_status
        if transient:
            assert work.next_attempt_at == NOW + timedelta(seconds=60)


def test_invalid_executor_result_blocks_work(tmp_path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_promote", "deployment-1", now=NOW)

        def invalid(owner, item, **kwargs):
            result = _complete(owner, item, **kwargs)
            return CandidatePromotionExecutionResult(
                result.deployment_id,
                "promotion_completed",
                "planned",
                result.replayed,
                result.work,
            )

        with pytest.raises(CandidatePromotionRetriggerError) as caught:
            run_candidate_promotion_retrigger_pass(
                store, TOKEN, reference_time=NOW, executor=invalid
            )
        assert caught.value.transient is False
