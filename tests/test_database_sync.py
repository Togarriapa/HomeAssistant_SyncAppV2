from pathlib import Path

import pytest
from ha_syncapp import database_sync
from ha_syncapp.database_snapshot import DatabaseSnapshot, DatabaseSnapshotError
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
DB_DIGEST = "d" * 64


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source_root = tmp_path / "homeassistant"
    database_staging = tmp_path / "database-staging"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    for path in (source_root, database_staging, snapshots, workspaces):
        path.mkdir()
    source = source_root / "home-assistant_v2.db"
    source.write_bytes(b"sqlite-source-placeholder")
    return source, database_staging, snapshots, workspaces


def _install_staging_fakes(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, object]],
) -> None:
    def database_capture(source: Path, root: Path) -> DatabaseSnapshot:
        calls.append(("database_capture", source))
        stage = root / ".snapshot-database-test.tmp"
        stage.mkdir()
        database = stage / "home-assistant_v2.db"
        database.write_bytes(b"consistent-database")
        return DatabaseSnapshot(stage, database, len(b"consistent-database"), DB_DIGEST)

    def snapshot_capture(source: Path, root: Path) -> Snapshot:
        calls.append(("snapshot_capture", source))
        stage = root / ".snapshot-test.tmp"
        tree = stage / "tree"
        tree.mkdir(parents=True)
        manifest = stage / "manifest.json"
        manifest.write_text("{}")
        return Snapshot("a" * 64, stage, tree, manifest, ())

    def prepare(snapshot_root: Path, root: Path) -> GitWorkspace:
        calls.append(("prepare", snapshot_root))
        stage = root / ".git-workspace-test.tmp"
        tree = stage / "tree"
        tree.mkdir(parents=True)
        return GitWorkspace("a" * 64, stage, tree)

    def initialize(workspace: GitWorkspace, *, default_branch: str = "main") -> object:
        calls.append(("initialize", default_branch))
        assert default_branch == "database"
        return object()

    monkeypatch.setattr(database_sync, "capture_sqlite_snapshot", database_capture)
    monkeypatch.setattr(database_sync, "capture_snapshot", snapshot_capture)
    monkeypatch.setattr(database_sync, "prepare_git_workspace", prepare)
    monkeypatch.setattr(database_sync, "initialize_repository", initialize)


def _head(commit_sha: str = OLD_SHA) -> BranchHead:
    return BranchHead(TARGET, REPOSITORY_ID, "database", commit_sha)


def _absence() -> BranchAbsence:
    return BranchAbsence(TARGET, REPOSITORY_ID, "database")


def _run(
    store: StateStore,
    tmp_path: Path,
) -> database_sync.DatabaseSyncResult:
    source, database_staging, snapshots, workspaces = _paths(tmp_path)
    return database_sync.synchronize_database_snapshot(
        store,
        source,
        database_staging,
        snapshots,
        workspaces,
        TARGET,
        "token",
    )


def test_existing_database_branch_without_baseline_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(database_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _head())
    monkeypatch.setattr(
        database_sync,
        "create_snapshot_commit",
        lambda workspace: pytest.fail("baseline-required database branch must not commit"),
    )
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is database_sync.DatabaseSyncDisposition.BASELINE_REQUIRED
    assert result.commit_sha is None
    assert result.database_sha256 == DB_DIGEST


def test_diverged_database_branch_is_blocked_before_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "database", "b" * 64, OLD_SHA)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        database_sync,
        "fetch_optional_trusted_branch_head",
        lambda *a, **k: _head(OTHER_SHA),
    )
    monkeypatch.setattr(
        database_sync,
        "anchor_trusted_baseline",
        lambda *a, **k: pytest.fail("diverged database branch must not be anchored"),
    )
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is database_sync.DatabaseSyncDisposition.DIVERGED


def test_missing_database_branch_after_baseline_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "database", "b" * 64, OLD_SHA)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(database_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _absence())
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is database_sync.DatabaseSyncDisposition.REMOTE_MISSING


def test_first_database_publication_uses_verified_publication_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(database_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _absence())
    monkeypatch.setattr(database_sync, "create_snapshot_commit", lambda workspace: NEW_SHA)
    intent = PublicationIntent(TARGET, REPOSITORY_ID, "database", NEW_SHA, None, True)
    monkeypatch.setattr(database_sync, "build_publication_intent", lambda workspace, preflight: intent)

    def complete(
        state: StateStore, workspace: GitWorkspace, supplied: PublicationIntent, token: str
    ):
        assert supplied == intent
        assert token == "token"
        return state.record_synchronization_baseline(
            TARGET, "database", workspace.snapshot_id, NEW_SHA
        )

    monkeypatch.setattr(database_sync, "complete_authorized_publication", complete)
    try:
        result = _run(store, tmp_path)
        persisted = store.synchronization_baseline(TARGET, "database")
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is database_sync.DatabaseSyncDisposition.INITIALIZED
    assert result.commit_sha == NEW_SHA
    assert persisted is not None and persisted.commit_sha == NEW_SHA


def test_existing_database_history_publishes_only_after_exact_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "database", "b" * 64, OLD_SHA)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(database_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _head())

    def anchor(workspace: GitWorkspace, remote: BranchHead, token: str) -> str:
        calls.append(("anchor", remote.commit_sha))
        return remote.commit_sha

    def commit(workspace: GitWorkspace) -> str:
        calls.append(("commit", workspace.snapshot_id))
        return NEW_SHA

    monkeypatch.setattr(database_sync, "anchor_trusted_baseline", anchor)
    monkeypatch.setattr(database_sync, "create_snapshot_commit", commit)
    monkeypatch.setattr(
        database_sync,
        "build_publication_intent",
        lambda workspace, preflight: PublicationIntent(
            TARGET, REPOSITORY_ID, "database", NEW_SHA, OLD_SHA, False
        ),
    )
    monkeypatch.setattr(
        database_sync,
        "complete_authorized_publication",
        lambda state, workspace, intent, token: state.record_synchronization_baseline(
            TARGET, "database", workspace.snapshot_id, NEW_SHA
        ),
    )
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is database_sync.DatabaseSyncDisposition.PUBLISHED
    names = [name for name, _ in calls]
    assert names.index("anchor") < names.index("commit")


def test_no_change_database_backup_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    baseline = store.record_synchronization_baseline(TARGET, "database", "b" * 64, OLD_SHA)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(database_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _head())
    monkeypatch.setattr(database_sync, "anchor_trusted_baseline", lambda *a, **k: OLD_SHA)
    monkeypatch.setattr(database_sync, "create_snapshot_commit", lambda workspace: None)
    monkeypatch.setattr(
        database_sync,
        "complete_authorized_publication",
        lambda *a, **k: pytest.fail("no-change database snapshot must not publish"),
    )
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is database_sync.DatabaseSyncDisposition.NO_CHANGE
    assert result.commit_sha == OLD_SHA
    assert result.baseline == baseline


def test_failure_cleans_all_database_staging_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    source, database_staging, snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        database_sync,
        "initialize_repository",
        lambda *a, **k: (_ for _ in ()).throw(GitError("expected test failure")),
    )
    try:
        with pytest.raises(database_sync.DatabaseSyncError, match="failed closed"):
            database_sync.synchronize_database_snapshot(
                store,
                source,
                database_staging,
                snapshots,
                workspaces,
                TARGET,
                "token",
            )
    finally:
        store.__exit__(None, None, None)

    assert list(database_staging.iterdir()) == []
    assert list(snapshots.iterdir()) == []
    assert list(workspaces.iterdir()) == []


def test_database_snapshot_failure_is_sanitized_and_never_enters_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    source, database_staging, snapshots, workspaces = _paths(tmp_path)
    secret_detail = "source-secret-detail"

    def fail(*args: object, **kwargs: object) -> DatabaseSnapshot:
        raise DatabaseSnapshotError(secret_detail)

    monkeypatch.setattr(database_sync, "capture_sqlite_snapshot", fail)
    monkeypatch.setattr(
        database_sync,
        "prepare_git_workspace",
        lambda *a, **k: pytest.fail("failed database capture must not reach Git"),
    )
    try:
        with pytest.raises(database_sync.DatabaseSyncError, match="failed closed") as error:
            database_sync.synchronize_database_snapshot(
                store,
                source,
                database_staging,
                snapshots,
                workspaces,
                TARGET,
                "token",
            )
    finally:
        store.__exit__(None, None, None)

    assert secret_detail not in str(error.value)
