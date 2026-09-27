from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from ha_syncapp.candidate_backup_execution import CandidateBackupExecutionError
from ha_syncapp.candidate_backup_retrigger import (
    CandidateBackupRetriggerError,
    run_candidate_backup_retrigger_pass,
)
from test_candidate_backup_execution import _backup_ready
from test_candidate_integrity_execution import NOW


def test_recovers_interrupted_backup_and_processes_exactly_one(tmp_path, monkeypatch) -> None:
    store, orchestration, _semantic, _authority = _backup_ready(tmp_path, monkeypatch)
    calls = []

    def execute(*args, **kwargs):
        calls.append((args[1].candidate_sha, kwargs["now"]))
        return SimpleNamespace(checkpoint=SimpleNamespace(phase="completed"))

    try:
        result = run_candidate_backup_retrigger_pass(
            store,
            staging_root=tmp_path,
            home_assistant_root=tmp_path,
            reference_time=NOW + timedelta(seconds=30),
            executor=execute,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 1
    assert result.considered == 1
    assert result.processed == "completed"
    assert calls == [(orchestration.candidate_sha, NOW + timedelta(seconds=30))]


def test_transient_failure_returns_claimed_work_to_controlled_backoff(
    tmp_path, monkeypatch
) -> None:
    store, orchestration, _semantic, _authority = _backup_ready(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise CandidateBackupExecutionError("secret transport detail", transient=True)

    try:
        with pytest.raises(CandidateBackupRetriggerError, match="failed closed"):
            run_candidate_backup_retrigger_pass(
                store,
                staging_root=tmp_path,
                home_assistant_root=tmp_path,
                reference_time=NOW + timedelta(seconds=30),
                executor=fail,
            )
        work = store._get_work("candidate", orchestration.candidate_sha)
    finally:
        store.__exit__(None, None, None)

    assert work.status == "retry"
    assert work.next_attempt_at == NOW + timedelta(seconds=150)
