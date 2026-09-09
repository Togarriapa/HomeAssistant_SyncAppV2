from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp.git_workspace import GitWorkspace, prepare_git_workspace
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.publication_result import PublicationResult
from ha_syncapp.publication_state import (
    PublicationStateError,
    record_verified_publication,
)
from ha_syncapp.snapshot import capture_snapshot
from ha_syncapp.state import StateStore

LOCAL = "b" * 40
BASELINE = "a" * 40
WHEN = datetime(2026, 9, 9, 20, 30, tzinfo=UTC)


def _workspace(tmp_path: Path) -> GitWorkspace:
    source = tmp_path / "ha"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    source.mkdir()
    snapshots.mkdir()
    workspaces.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    snapshot = capture_snapshot(source, snapshots)
    return prepare_git_workspace(snapshot.root, workspaces)


def _intent() -> PublicationIntent:
    return PublicationIntent(
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=LOCAL,
        expected_remote_commit_sha=BASELINE,
        expect_remote_absent=False,
    )


def _result(
    *,
    target: str = "Owner/Home",
    repository_id: int = 42,
    branch: str = "main",
    commit_sha: str = LOCAL,
) -> PublicationResult:
    return PublicationResult(target, repository_id, branch, commit_sha)


def test_verified_publication_advances_exact_snapshot_baseline(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 42)

        baseline = record_verified_publication(
            store,
            workspace,
            _intent(),
            _result(),
            synchronized_at=WHEN,
        )

        assert baseline.target == "Owner/Home"
        assert baseline.branch == "main"
        assert baseline.snapshot_id == workspace.snapshot_id
        assert baseline.commit_sha == LOCAL
        assert baseline.synchronized_at == WHEN
        assert store.synchronization_baseline("Owner/Home", "main") == baseline


def test_replaying_same_verified_publication_is_idempotent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 42)
        first = record_verified_publication(
            store, workspace, _intent(), _result(), synchronized_at=WHEN
        )
        second = record_verified_publication(
            store, workspace, _intent(), _result(), synchronized_at=WHEN
        )

        assert second == first


def test_mutated_workspace_never_advances_baseline(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace.tree_path / "configuration.yaml").write_text("homeassistant:\n  name: changed\n")
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 42)

        with pytest.raises(PublicationStateError, match="could not be re-proven"):
            record_verified_publication(store, workspace, _intent(), _result())

        assert store.synchronization_baseline("Owner/Home", "main") is None


def test_missing_repository_binding_never_advances_baseline(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with StateStore(tmp_path) as store:
        with pytest.raises(PublicationStateError, match="binding is missing or changed"):
            record_verified_publication(store, workspace, _intent(), _result())

        assert store.synchronization_baseline("Owner/Home", "main") is None


def test_wrong_repository_binding_never_advances_baseline(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 99)

        with pytest.raises(PublicationStateError, match="binding is missing or changed"):
            record_verified_publication(store, workspace, _intent(), _result())

        assert store.synchronization_baseline("Owner/Home", "main") is None


@pytest.mark.parametrize(
    "result",
    [
        _result(target="Other/Home"),
        _result(repository_id=99),
        _result(branch="candidate"),
        _result(commit_sha="c" * 40),
    ],
)
def test_forged_publication_result_never_advances_baseline(
    tmp_path: Path, result: PublicationResult
) -> None:
    workspace = _workspace(tmp_path)
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 42)

        with pytest.raises(PublicationStateError, match="could not be re-proven"):
            record_verified_publication(store, workspace, _intent(), result)

        assert store.synchronization_baseline("Owner/Home", "main") is None
