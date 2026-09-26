from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from ha_syncapp.candidate_fetch_stage_execution import (
    CandidateFetchStageExecutionError,
    CandidateFetchStageResult,
)
from ha_syncapp.candidate_fetch_stage_retrigger import (
    CandidateFetchStageRetriggerError,
    run_candidate_fetch_stage_retrigger_pass,
)
from ha_syncapp.candidate_orchestration import (
    CandidateOrchestrationError,
    load_candidate_orchestration,
)
from ha_syncapp.state import StateStore

NOW = datetime(2026, 9, 26, 16, 30, tzinfo=UTC)
TARGET = "owner/home-assistant-config"
TOKEN = "github-secret"
REPOSITORY_ID = 42
SHA_A = "a" * 40
SHA_B = "b" * 40


def _ready(tmp_path: Path) -> tuple[StateStore, Path, Path, Path]:
    home = tmp_path / "homeassistant"
    workspace = tmp_path / "candidate-workspace"
    staging = tmp_path / "candidate-staging"
    home.mkdir(mode=0o700)
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store, home, workspace, staging


def _completed_result() -> CandidateFetchStageResult:
    return cast(
        CandidateFetchStageResult,
        SimpleNamespace(checkpoint=SimpleNamespace(phase="completed")),
    )


def test_claims_registers_and_defers_one_candidate_for_next_phase(tmp_path: Path) -> None:
    store, home, workspace, staging = _ready(tmp_path)
    store.enqueue_work("candidate", SHA_A, now=NOW)
    calls: list[tuple[object, ...]] = []

    def execute(*args: object, **kwargs: object) -> CandidateFetchStageResult:
        calls.append((args, kwargs))
        return _completed_result()

    try:
        result = run_candidate_fetch_stage_retrigger_pass(
            store,
            home,
            workspace,
            staging,
            TARGET,
            TOKEN,
            reference_time=NOW,
            executor=execute,
        )
        work = store._get_work("candidate", SHA_A)
        orchestration = load_candidate_orchestration(store, SHA_A)
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 0
    assert result.considered == 1
    assert result.processed == "completed"
    assert len(calls) == 1
    assert work.status == "pending"
    assert work.attempts == 0
    assert work.next_attempt_at == NOW
    assert orchestration is not None
    assert orchestration.phase == "detected"
    args, kwargs = calls[0]
    assert args == (store, orchestration)
    assert kwargs == {
        "token": TOKEN,
        "workspace_root": workspace,
        "staging_root": staging,
        "home_assistant_root": home,
        "now": NOW,
    }


@pytest.mark.parametrize(
    ("transient", "status", "next_attempt_at"),
    [
        (True, "retry", NOW + timedelta(seconds=60)),
        (False, "blocked", None),
    ],
)
def test_classifies_failure_without_leaking_nested_detail(
    tmp_path: Path,
    transient: bool,
    status: str,
    next_attempt_at: datetime | None,
) -> None:
    store, home, workspace, staging = _ready(tmp_path)
    store.enqueue_work("candidate", SHA_A, now=NOW)

    def fail(*args: object, **kwargs: object) -> CandidateFetchStageResult:
        raise CandidateFetchStageExecutionError(
            "github-secret nested candidate detail", transient=transient
        )

    try:
        with pytest.raises(
            CandidateFetchStageRetriggerError,
            match="candidate Fetch/Stage failed closed",
        ) as caught:
            run_candidate_fetch_stage_retrigger_pass(
                store,
                home,
                workspace,
                staging,
                TARGET,
                TOKEN,
                reference_time=NOW,
                executor=fail,
            )
        work = store._get_work("candidate", SHA_A)
    finally:
        store.__exit__(None, None, None)

    assert work.status == status
    assert work.attempts == 1
    assert work.next_attempt_at == next_attempt_at
    assert "github-secret" not in str(caught.value)


def test_recovers_interrupted_candidate_and_executes_it_once(tmp_path: Path) -> None:
    store, home, workspace, staging = _ready(tmp_path)
    store.enqueue_work("candidate", SHA_A, now=NOW)
    claimed = store.claim_work_kind("candidate", now=NOW)
    assert claimed is not None and claimed.status == "running"
    calls = 0

    def execute(*args: object, **kwargs: object) -> CandidateFetchStageResult:
        nonlocal calls
        calls += 1
        return _completed_result()

    try:
        result = run_candidate_fetch_stage_retrigger_pass(
            store,
            home,
            workspace,
            staging,
            TARGET,
            TOKEN,
            reference_time=NOW + timedelta(seconds=1),
            executor=execute,
        )
        work = store._get_work("candidate", SHA_A)
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 1
    assert calls == 1
    assert work.status == "pending"
    assert work.attempts == 0


def test_processes_at_most_one_eligible_candidate_per_pass(tmp_path: Path) -> None:
    store, home, workspace, staging = _ready(tmp_path)
    store.enqueue_work("candidate", SHA_A, now=NOW)
    store.enqueue_work("candidate", SHA_B, now=NOW)
    calls = 0

    def execute(*args: object, **kwargs: object) -> CandidateFetchStageResult:
        nonlocal calls
        calls += 1
        return _completed_result()

    try:
        result = run_candidate_fetch_stage_retrigger_pass(
            store,
            home,
            workspace,
            staging,
            TARGET,
            TOKEN,
            reference_time=NOW,
            executor=execute,
        )
        states = {sha: store._get_work("candidate", sha) for sha in (SHA_A, SHA_B)}
        orchestrations = {sha: load_candidate_orchestration(store, sha) for sha in (SHA_A, SHA_B)}
    finally:
        store.__exit__(None, None, None)

    assert result.considered == 2
    assert calls == 1
    assert all(item.status == "pending" for item in states.values())
    assert sum(item.attempts for item in states.values()) == 0
    assert sum(record is not None for record in orchestrations.values()) == 1


def test_rejects_workspace_symlink_without_changing_its_target(tmp_path: Path) -> None:
    store, home, workspace, staging = _ready(tmp_path)
    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    workspace.symlink_to(target, target_is_directory=True)
    original_mode = stat.S_IMODE(target.stat().st_mode)

    try:
        with pytest.raises(CandidateFetchStageRetriggerError, match="roots are unsafe"):
            run_candidate_fetch_stage_retrigger_pass(
                store,
                home,
                workspace,
                staging,
                TARGET,
                TOKEN,
                reference_time=NOW,
            )
    finally:
        store.__exit__(None, None, None)

    assert stat.S_IMODE(target.stat().st_mode) == original_mode


def test_corrupt_orchestration_blocks_exact_claim_without_retry_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, home, workspace, staging = _ready(tmp_path)
    store.enqueue_work("candidate", SHA_A, now=NOW)

    def corrupt(*args: object, **kwargs: object) -> None:
        raise CandidateOrchestrationError("secret corrupt evidence")

    monkeypatch.setattr(
        "ha_syncapp.candidate_fetch_stage_retrigger.load_candidate_orchestration",
        corrupt,
    )
    try:
        with pytest.raises(CandidateFetchStageRetriggerError, match="failed closed") as caught:
            run_candidate_fetch_stage_retrigger_pass(
                store,
                home,
                workspace,
                staging,
                TARGET,
                TOKEN,
                reference_time=NOW,
            )
        work = store._get_work("candidate", SHA_A)
    finally:
        store.__exit__(None, None, None)

    assert work.status == "blocked"
    assert "secret" not in str(caught.value)
