from pathlib import Path

import pytest
from ha_syncapp import local_sync
from ha_syncapp.git_workspace import GitWorkspace
from ha_syncapp.github_repo import BranchAbsence, BranchHead
from ha_syncapp.local_git import GitError
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.snapshot import Snapshot
from ha_syncapp.state import StateStore

TARGET = "example/private-home-assistant"
REPOSITORY_ID = 12345
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
OTHER_SHA = "3" * 40


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "homeassistant"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    for path in (source, snapshots, workspaces):
        path.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    return source, snapshots, workspaces


def _install_isolated_staging_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    calls: list[tuple[str, object]],
) -> None:
    def capture(source: Path, root: Path) -> Snapshot:
        calls.append(("capture", source))
        snapshot_dir = root / ".snapshot-test.tmp"
        tree = snapshot_dir / "tree"
        tree.mkdir(parents=True)
        manifest = snapshot_dir / "manifest.json"
        manifest.write_text("{}")
        return Snapshot("a" * 64, snapshot_dir, tree, manifest, ())

    def prepare(snapshot_root: Path, root: Path) -> GitWorkspace:
        calls.append(("prepare", snapshot_root))
        workspace_dir = root / ".git-workspace-test.tmp"
        tree = workspace_dir / "tree"
        tree.mkdir(parents=True)
        return GitWorkspace("a" * 64, workspace_dir, tree)

    def initialize(workspace: GitWorkspace, *, default_branch: str = "main") -> object:
        calls.append(("initialize", workspace.tree_path))
        assert workspace.tree_path != tmp_path / "homeassistant"
        assert default_branch == "main"
        return object()

    monkeypatch.setattr(local_sync, "capture_snapshot", capture)
    monkeypatch.setattr(local_sync, "prepare_git_workspace", prepare)
    monkeypatch.setattr(local_sync, "initialize_repository", initialize)


def _remote_head(commit_sha: str = OLD_SHA) -> BranchHead:
    return BranchHead(TARGET, REPOSITORY_ID, "main", commit_sha)


def _remote_absence() -> BranchAbsence:
    return BranchAbsence(TARGET, REPOSITORY_ID, "main")


def test_existing_remote_without_baseline_is_blocked_before_git_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    source, snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_isolated_staging_fakes(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(local_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _remote_head())
    monkeypatch.setattr(
        local_sync,
        "create_snapshot_commit",
        lambda workspace: pytest.fail("baseline-required state must not create a commit"),
    )

    try:
        result = local_sync.synchronize_local_configuration(
            store, source, snapshots, workspaces, TARGET, "token"
        )
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is local_sync.LocalSyncDisposition.BASELINE_REQUIRED
    assert result.commit_sha is None
    assert [name for name, _ in calls] == ["capture", "prepare", "initialize"]
    assert list(snapshots.iterdir()) == []
    assert list(workspaces.iterdir()) == []


def test_diverged_remote_is_blocked_before_anchor_or_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "main", "b" * 64, OLD_SHA)
    source, snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_isolated_staging_fakes(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        local_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _remote_head(OTHER_SHA)
    )
    monkeypatch.setattr(
        local_sync,
        "anchor_trusted_baseline",
        lambda *a, **k: pytest.fail("divergence must not be anchored"),
    )

    try:
        result = local_sync.synchronize_local_configuration(
            store, source, snapshots, workspaces, TARGET, "token"
        )
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is local_sync.LocalSyncDisposition.DIVERGED
    assert store is not None


def test_missing_remote_after_baseline_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "main", "b" * 64, OLD_SHA)
    source, snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_isolated_staging_fakes(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        local_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _remote_absence()
    )

    try:
        result = local_sync.synchronize_local_configuration(
            store, source, snapshots, workspaces, TARGET, "token"
        )
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is local_sync.LocalSyncDisposition.REMOTE_MISSING


def test_first_publication_uses_existing_authorization_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    source, snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_isolated_staging_fakes(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        local_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _remote_absence()
    )
    monkeypatch.setattr(local_sync, "create_snapshot_commit", lambda workspace: NEW_SHA)

    intent = PublicationIntent(TARGET, REPOSITORY_ID, "main", NEW_SHA, None, True)

    def build(workspace: GitWorkspace, preflight: object) -> PublicationIntent:
        calls.append(("intent", preflight))
        return intent

    def complete(
        state: StateStore, workspace: GitWorkspace, supplied: PublicationIntent, token: str
    ) -> object:
        calls.append(("publish", supplied))
        assert token == "token"
        return state.record_synchronization_baseline(TARGET, "main", workspace.snapshot_id, NEW_SHA)

    monkeypatch.setattr(local_sync, "build_publication_intent", build)
    monkeypatch.setattr(local_sync, "complete_authorized_publication", complete)

    try:
        result = local_sync.synchronize_local_configuration(
            store, source, snapshots, workspaces, TARGET, "token"
        )
        persisted = store.synchronization_baseline(TARGET, "main")
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is local_sync.LocalSyncDisposition.INITIALIZED
    assert result.commit_sha == NEW_SHA
    assert persisted is not None and persisted.commit_sha == NEW_SHA
    assert [name for name, _ in calls][-2:] == ["intent", "publish"]


def test_normal_publication_anchors_exact_baseline_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "main", "b" * 64, OLD_SHA)
    source, snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_isolated_staging_fakes(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(local_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _remote_head())

    def anchor(workspace: GitWorkspace, remote: BranchHead, token: str) -> str:
        calls.append(("anchor", remote.commit_sha))
        return remote.commit_sha

    def commit(workspace: GitWorkspace) -> str:
        calls.append(("commit", workspace.snapshot_id))
        return NEW_SHA

    monkeypatch.setattr(local_sync, "anchor_trusted_baseline", anchor)
    monkeypatch.setattr(local_sync, "create_snapshot_commit", commit)
    monkeypatch.setattr(
        local_sync,
        "build_publication_intent",
        lambda workspace, preflight: PublicationIntent(
            TARGET, REPOSITORY_ID, "main", NEW_SHA, OLD_SHA, False
        ),
    )
    monkeypatch.setattr(
        local_sync,
        "complete_authorized_publication",
        lambda state, workspace, intent, token: state.record_synchronization_baseline(
            TARGET, "main", workspace.snapshot_id, NEW_SHA
        ),
    )

    try:
        result = local_sync.synchronize_local_configuration(
            store, source, snapshots, workspaces, TARGET, "token"
        )
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is local_sync.LocalSyncDisposition.PUBLISHED
    assert [name for name, _ in calls].index("anchor") < [name for name, _ in calls].index("commit")


def test_no_change_never_invokes_publication_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    baseline = store.record_synchronization_baseline(TARGET, "main", "b" * 64, OLD_SHA)
    source, snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_isolated_staging_fakes(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(local_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _remote_head())
    monkeypatch.setattr(local_sync, "anchor_trusted_baseline", lambda *a, **k: OLD_SHA)
    monkeypatch.setattr(local_sync, "create_snapshot_commit", lambda workspace: None)
    monkeypatch.setattr(
        local_sync,
        "complete_authorized_publication",
        lambda *a, **k: pytest.fail("no-change cycle must not publish"),
    )

    try:
        result = local_sync.synchronize_local_configuration(
            store, source, snapshots, workspaces, TARGET, "token"
        )
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is local_sync.LocalSyncDisposition.NO_CHANGE
    assert result.commit_sha == OLD_SHA
    assert result.baseline == baseline


def test_failure_removes_temporary_snapshot_and_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    source, snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_isolated_staging_fakes(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        local_sync,
        "initialize_repository",
        lambda *a, **k: (_ for _ in ()).throw(GitError("expected test failure")),
    )

    try:
        with pytest.raises(local_sync.LocalSyncError, match="failed closed"):
            local_sync.synchronize_local_configuration(
                store, source, snapshots, workspaces, TARGET, "token"
            )
    finally:
        store.__exit__(None, None, None)

    assert list(snapshots.iterdir()) == []
    assert list(workspaces.iterdir()) == []
