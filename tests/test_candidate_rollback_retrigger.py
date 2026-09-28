from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from ha_syncapp.candidate_rollback_execution import (
    CandidateRollbackExecutionError,
    CandidateRollbackExecutionResult,
)
from ha_syncapp.candidate_rollback_retrigger import (
    CandidateRollbackRetriggerError,
    run_candidate_rollback_retrigger_pass,
)
from ha_syncapp.deployment_rollback import RollbackBackupProof, RollbackRepositoryProof
from ha_syncapp.state import StateStore

NOW = datetime(2031, 2, 3, 4, 20, tzinfo=UTC)
GITHUB_TOKEN = "github-secret-sentinel"
SUPERVISOR_TOKEN = "supervisor-secret-sentinel"


def _repository(*_args):
    return RollbackRepositoryProof(1, True, "a" * 40)


def _backup(*_args):
    return RollbackBackupProof("backup-1", "full", "2031.2.3", True, True)


def _complete(store, item, **kwargs):
    work = store.complete_work(item, now=kwargs["now"])
    return CandidateRollbackExecutionResult(
        item.work_key,
        "rollback_authorized",
        "planned",
        False,
        work,
    )


def test_pass_recovers_stale_work_and_processes_only_one(tmp_path) -> None:
    processed: list[str] = []
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_rollback", "deployment-1", now=NOW)
        store.enqueue_work("candidate_rollback", "deployment-2", now=NOW)
        stale = store.claim_work_kind("candidate_rollback", now=NOW)
        assert stale is not None

        def execute(owner, item, **kwargs):
            assert kwargs["github_token"] == GITHUB_TOKEN
            assert kwargs["supervisor_token"] == SUPERVISOR_TOKEN
            assert kwargs["repository_reader"] is _repository
            assert kwargs["backup_reader"] is _backup
            processed.append(item.work_key)
            return _complete(owner, item, **kwargs)

        result = run_candidate_rollback_retrigger_pass(
            store,
            GITHUB_TOKEN,
            SUPERVISOR_TOKEN,
            repository_reader=_repository,
            backup_reader=_backup,
            reference_time=NOW,
            executor=execute,
        )
        assert result.recovered_interrupted == 1
        assert result.considered == 2
        assert result.processed == "rollback_authorized"
        assert len(processed) == 1


@pytest.mark.parametrize(
    ("transient", "expected_status"),
    [(True, "retry"), (False, "blocked")],
)
def test_failure_classification_controls_retry(
    tmp_path, transient: bool, expected_status: str
) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_rollback", "deployment-1", now=NOW)

        def fail(*_args, **_kwargs):
            raise CandidateRollbackExecutionError("sanitized", transient=transient)

        with pytest.raises(CandidateRollbackRetriggerError) as caught:
            run_candidate_rollback_retrigger_pass(
                store,
                GITHUB_TOKEN,
                SUPERVISOR_TOKEN,
                repository_reader=_repository,
                backup_reader=_backup,
                reference_time=NOW,
                executor=fail,
            )
        assert caught.value.transient is transient
        work = store._get_work("candidate_rollback", "deployment-1")
        assert work.status == expected_status
        if transient:
            assert work.next_attempt_at == NOW + timedelta(seconds=60)


def test_invalid_executor_result_blocks_work(tmp_path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("candidate_rollback", "deployment-1", now=NOW)

        def invalid(owner, item, **kwargs):
            result = _complete(owner, item, **kwargs)
            return CandidateRollbackExecutionResult(
                result.deployment_id,
                "rollback_authorized",
                "invalid",
                result.replayed,
                result.work,
            )

        with pytest.raises(CandidateRollbackRetriggerError) as caught:
            run_candidate_rollback_retrigger_pass(
                store,
                GITHUB_TOKEN,
                SUPERVISOR_TOKEN,
                repository_reader=_repository,
                backup_reader=_backup,
                reference_time=NOW,
                executor=invalid,
            )
        assert caught.value.transient is False
