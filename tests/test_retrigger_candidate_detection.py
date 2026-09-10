from pathlib import Path

import pytest
from ha_syncapp import candidate_detection, retrigger_cycle
from ha_syncapp.database_sync_retrigger import DatabaseSyncRetriggerResult
from ha_syncapp.github_repo import BranchHead, RepositoryVerificationError
from ha_syncapp.local_sync_retrigger import LocalSyncRetriggerResult
from ha_syncapp.runtime_sync_retrigger import RuntimeSyncRetriggerResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
CANDIDATE_SHA = "a" * 40


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store


def _patch_recovery_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        retrigger_cycle,
        "run_local_sync_retrigger_pass",
        lambda *args, **kwargs: LocalSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_database_sync_retrigger_pass",
        lambda *args, **kwargs: DatabaseSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_runtime_sync_retrigger_pass",
        lambda *args, **kwargs: RuntimeSyncRetriggerResult(0, None),
    )


def _run(store: StateStore, tmp_path: Path):
    home = tmp_path / "homeassistant"
    home.mkdir(exist_ok=True)
    database = home / "home-assistant_v2.db"
    database.touch(exist_ok=True)
    return retrigger_cycle.run_retrigger_cycle(
        store,
        home,
        tmp_path / "snapshots",
        tmp_path / "local-workspaces",
        database,
        tmp_path / "database-staging",
        tmp_path / "database-snapshots",
        tmp_path / "database-workspaces",
        tmp_path / "runtime-staging",
        tmp_path / "runtime-snapshots",
        tmp_path / "runtime-workspaces",
        TARGET,
        "github-token",
    )


def test_retrigger_enqueues_exact_trusted_candidate_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _patch_recovery_lanes(monkeypatch)

    def fetch(target: str, token: str, *, expected_id: int, branch: str) -> BranchHead:
        assert (target, token, expected_id, branch) == (
            TARGET,
            "github-token",
            REPOSITORY_ID,
            "candidate",
        )
        return BranchHead(TARGET, REPOSITORY_ID, "candidate", CANDIDATE_SHA)

    monkeypatch.setattr(candidate_detection, "fetch_optional_trusted_branch_head", fetch)
    try:
        result = _run(store, tmp_path)
        claimed = store.claim_work_kind("candidate")
    finally:
        store.__exit__(None, None, None)

    assert result.candidate_detection.observation.commit_sha == CANDIDATE_SHA
    assert result.candidate_detection.work is not None
    assert result.candidate_detection.work.work_key == CANDIDATE_SHA
    assert claimed is not None and claimed.work_key == CANDIDATE_SHA


@pytest.mark.parametrize("terminal_status", ["succeeded", "blocked"])
def test_retrigger_does_not_rearm_identical_terminal_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
) -> None:
    store = _store(tmp_path)
    _patch_recovery_lanes(monkeypatch)
    original = store.enqueue_work("candidate", CANDIDATE_SHA)
    claimed = store.claim_work_kind("candidate")
    assert claimed is not None
    if terminal_status == "succeeded":
        terminal = store.complete_work(claimed)
    else:
        terminal = store.fail_work(claimed, transient=False)

    monkeypatch.setattr(
        candidate_detection,
        "fetch_optional_trusted_branch_head",
        lambda *args, **kwargs: BranchHead(
            TARGET,
            REPOSITORY_ID,
            "candidate",
            CANDIDATE_SHA,
        ),
    )
    try:
        result = _run(store, tmp_path)
        eligible = store.claim_work_kind("candidate")
    finally:
        store.__exit__(None, None, None)

    assert original.work_key == CANDIDATE_SHA
    assert result.candidate_detection.work == terminal
    assert result.candidate_detection.work.status == terminal_status
    assert eligible is None


def test_repository_failure_is_sanitized_by_retrigger_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    _patch_recovery_lanes(monkeypatch)
    secret = "github-secret-sentinel"

    def fail(*args: object, **kwargs: object) -> BranchHead:
        raise RepositoryVerificationError(secret)

    monkeypatch.setattr(candidate_detection, "fetch_optional_trusted_branch_head", fail)
    try:
        with pytest.raises(retrigger_cycle.RetriggerCycleError) as caught:
            _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert str(caught.value) == "retrigger cycle failed closed"
    assert secret not in str(caught.value)
