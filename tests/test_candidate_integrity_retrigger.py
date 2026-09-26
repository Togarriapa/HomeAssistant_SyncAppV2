from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from ha_syncapp.candidate_integrity_execution import CandidateIntegrityExecutionError
from ha_syncapp.candidate_integrity_retrigger import (
    CandidateIntegrityRetriggerError,
    run_candidate_integrity_retrigger_pass,
)
from ha_syncapp.candidate_orchestration import (
    register_claimed_candidate,
    staged_candidate_orchestration,
)
from ha_syncapp.state import StateStore

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
TARGET = "owner/home-assistant-config"
SHA = "a" * 40


def _ready(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 42)
    store.enqueue_work("candidate", SHA, now=NOW)
    claimed = store.claim_work_kind("candidate", now=NOW)
    assert claimed is not None
    detected = register_claimed_candidate(store, claimed, target=TARGET, repository_id=42, now=NOW)
    staged = staged_candidate_orchestration(detected, updated_at=NOW)
    store._connection.execute(
        "UPDATE candidate_orchestration SET phase = ?, next_action = ?, updated_at = ?, "
        "record_sha256 = ? WHERE candidate_sha = ?",
        (
            staged.phase,
            staged.next_action,
            staged.updated_at.isoformat(),
            staged.record_sha256,
            SHA,
        ),
    )
    store._connection.commit()
    store.defer_work(claimed, now=NOW)
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    staging = tmp_path / "staging"
    for root in (home, workspace, staging):
        root.mkdir()
    return store, home, workspace, staging


def test_claims_only_analyze_action_and_defers_success(tmp_path: Path) -> None:
    store, home, workspace, staging = _ready(tmp_path)
    calls: list[str] = []

    def execute(store_arg, orchestration, **kwargs):
        calls.append(orchestration.next_action)
        assert store_arg is store
        assert kwargs["token"] == "token"
        return SimpleNamespace(checkpoint=SimpleNamespace(phase="completed"))

    try:
        result = run_candidate_integrity_retrigger_pass(
            store, home, workspace, staging, "token", reference_time=NOW, executor=execute
        )
        work = store._get_work("candidate", SHA)
    finally:
        store.__exit__(None, None, None)
    assert calls == ["analyze"]
    assert result.considered == 1
    assert result.processed == "completed"
    assert work.status == "pending"


@pytest.mark.parametrize("transient, expected", [(True, "retry"), (False, "blocked")])
def test_failure_class_controls_backoff_or_block(
    tmp_path: Path, transient: bool, expected: str
) -> None:
    store, home, workspace, staging = _ready(tmp_path)

    def fail(*args, **kwargs):
        raise CandidateIntegrityExecutionError("secret detail", transient=transient)

    try:
        with pytest.raises(CandidateIntegrityRetriggerError, match="failed closed") as error:
            run_candidate_integrity_retrigger_pass(
                store, home, workspace, staging, "token", reference_time=NOW, executor=fail
            )
        work = store._get_work("candidate", SHA)
    finally:
        store.__exit__(None, None, None)
    assert "secret" not in str(error.value)
    assert work.status == expected


def test_recovers_interrupted_analyze_work_before_claim(tmp_path: Path) -> None:
    store, home, workspace, staging = _ready(tmp_path)
    pending = store.claim_work_kind("candidate", now=NOW)
    assert pending is not None and pending.status == "running"
    try:
        result = run_candidate_integrity_retrigger_pass(
            store,
            home,
            workspace,
            staging,
            "token",
            reference_time=NOW,
            executor=lambda *args, **kwargs: SimpleNamespace(
                checkpoint=SimpleNamespace(phase="completed")
            ),
        )
    finally:
        store.__exit__(None, None, None)
    assert result.recovered_interrupted == 1
    assert result.processed == "completed"
