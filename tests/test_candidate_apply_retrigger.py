from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ha_syncapp.candidate_apply_admission import (
    CandidateApplyAdmissionError,
    CandidateApplyAdmissionResult,
)
from ha_syncapp.candidate_apply_retrigger import (
    CandidateApplyRetriggerError,
    run_candidate_apply_retrigger_pass,
)
from ha_syncapp.state import StateStore

NOW = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)


def _complete(store, item, **kwargs):
    store.complete_work(item, now=kwargs["now"])
    return CandidateApplyAdmissionResult(item.work_key, None)  # type: ignore[arg-type]


def test_pass_claims_one_exact_apply_work_and_forwards_credentials(tmp_path) -> None:
    calls = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_apply", "deployment-1", now=NOW)

        def execute(owner, item, **kwargs):
            calls.append((item, kwargs))
            return _complete(owner, item, **kwargs)

        result = run_candidate_apply_retrigger_pass(
            store,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            github_token="github-token",
            supervisor_token="supervisor-token",
            reference_time=NOW,
            executor=execute,
        )

        assert result.recovered_interrupted == 0
        assert result.considered == 1
        assert result.processed == "admitted"
        item, arguments = calls[0]
        assert item.status == "running" and item.attempts == 1
        assert arguments["github_token"] == "github-token"
        assert arguments["supervisor_token"] == "supervisor-token"
        assert store._get_work("candidate_apply", "deployment-1").status == "succeeded"


@pytest.mark.parametrize(
    ("transient", "expected_status"),
    [(True, "retry"), (False, "blocked")],
)
def test_failure_classification_controls_retry_or_block(
    tmp_path, transient, expected_status
) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_apply", "deployment-1", now=NOW)

        def fail(*args, **kwargs):
            raise CandidateApplyAdmissionError("sanitized", transient=transient)

        with pytest.raises(CandidateApplyRetriggerError):
            run_candidate_apply_retrigger_pass(
                store,
                staging_root=tmp_path,
                home_assistant_root=tmp_path,
                github_token="github-token",
                supervisor_token="supervisor-token",
                reference_time=NOW,
                executor=fail,
            )

        failed = store._get_work("candidate_apply", "deployment-1")
        assert failed.status == expected_status
        assert (failed.next_attempt_at is not None) is transient


def test_stale_running_apply_work_is_recovered_before_claim(tmp_path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_apply", "deployment-1", now=NOW)
        stale = store.claim_work_kind("candidate_apply", now=NOW)
        assert stale is not None and stale.attempts == 1

        result = run_candidate_apply_retrigger_pass(
            store,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            github_token="github-token",
            supervisor_token="supervisor-token",
            reference_time=NOW,
            executor=_complete,
        )

        assert result.recovered_interrupted == 1
        assert result.processed == "admitted"
        assert store._get_work("candidate_apply", "deployment-1").attempts == 2


def test_pass_is_bounded_to_one_apply_action(tmp_path) -> None:
    processed = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_apply", "deployment-1", now=NOW)
        store.enqueue_work("candidate_apply", "deployment-2", now=NOW)

        def execute(owner, item, **kwargs):
            processed.append(item.work_key)
            return _complete(owner, item, **kwargs)

        result = run_candidate_apply_retrigger_pass(
            store,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            github_token="github-token",
            supervisor_token="supervisor-token",
            reference_time=NOW,
            executor=execute,
        )

        assert result.considered == 2
        assert len(processed) == 1
        statuses = {
            store._get_work("candidate_apply", key).status
            for key in ("deployment-1", "deployment-2")
        }
        assert statuses == {"pending", "succeeded"}
