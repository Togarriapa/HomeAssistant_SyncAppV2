from pathlib import Path

import pytest
from ha_syncapp import runtime_sync
from ha_syncapp.git_workspace import GitWorkspace
from ha_syncapp.github_repo import BranchAbsence, BranchHead
from ha_syncapp.local_git import GitError
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.runtime_inventory import RuntimeInventoryArtifact, RuntimeInventoryInput
from ha_syncapp.snapshot import Snapshot
from ha_syncapp.state import StateStore, SynchronizationBaseline

TARGET = "example/private-home-assistant"
REPOSITORY_ID = 12345
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
OTHER_SHA = "3" * 40
ARTIFACT_ID = "a" * 64
SNAPSHOT_ID = "b" * 64


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    runtime_staging = tmp_path / "runtime-staging"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    for path in (runtime_staging, snapshots, workspaces):
        path.mkdir()
    return runtime_staging, snapshots, workspaces


def _inventory() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={"home_assistant_version": "2026.9.0"},
        homeassistant={"entities": [{"entity_id": "light.kitchen"}]},
    )


def _install_staging_fakes(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, object]],
) -> None:
    def build(root: Path, inventory: RuntimeInventoryInput) -> RuntimeInventoryArtifact:
        calls.append(("build", root))
        stage = root / ARTIFACT_ID
        stage.mkdir()
        (stage / "manifest.json").write_text("{}\n")
        return RuntimeInventoryArtifact(stage, ARTIFACT_ID, ())

    def verify(artifact: RuntimeInventoryArtifact) -> None:
        calls.append(("verify", artifact.artifact_id))

    def snapshot_capture(source: Path, root: Path) -> Snapshot:
        calls.append(("snapshot_capture", source))
        stage = root / ".snapshot-test.tmp"
        tree = stage / "tree"
        tree.mkdir(parents=True)
        manifest = stage / "manifest.json"
        manifest.write_text("{}")
        return Snapshot(SNAPSHOT_ID, stage, tree, manifest, ())

    def prepare(snapshot_root: Path, root: Path) -> GitWorkspace:
        calls.append(("prepare", snapshot_root))
        stage = root / ".git-workspace-test.tmp"
        tree = stage / "tree"
        tree.mkdir(parents=True)
        return GitWorkspace(SNAPSHOT_ID, stage, tree)

    def initialize(workspace: GitWorkspace, *, default_branch: str = "main") -> object:
        calls.append(("initialize", default_branch))
        assert default_branch == "runtime"
        return object()

    monkeypatch.setattr(runtime_sync, "build_runtime_inventory", build)
    monkeypatch.setattr(runtime_sync, "verify_runtime_inventory", verify)
    monkeypatch.setattr(runtime_sync, "capture_snapshot", snapshot_capture)
    monkeypatch.setattr(runtime_sync, "prepare_git_workspace", prepare)
    monkeypatch.setattr(runtime_sync, "initialize_repository", initialize)


def _head(commit_sha: str = OLD_SHA) -> BranchHead:
    return BranchHead(TARGET, REPOSITORY_ID, "runtime", commit_sha)


def _absence() -> BranchAbsence:
    return BranchAbsence(TARGET, REPOSITORY_ID, "runtime")


def _run(store: StateStore, tmp_path: Path) -> runtime_sync.RuntimeSyncResult:
    runtime_staging, snapshots, workspaces = _paths(tmp_path)
    return runtime_sync.synchronize_runtime_inventory(
        store,
        _inventory(),
        runtime_staging,
        snapshots,
        workspaces,
        TARGET,
        "token",
    )


def test_existing_runtime_branch_without_baseline_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(runtime_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _head())
    monkeypatch.setattr(
        runtime_sync,
        "create_snapshot_commit",
        lambda workspace: pytest.fail("baseline-required runtime branch must not commit"),
    )
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is runtime_sync.RuntimeSyncDisposition.BASELINE_REQUIRED
    assert result.artifact_id == ARTIFACT_ID
    assert result.commit_sha is None


def test_diverged_runtime_branch_is_blocked_before_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "runtime", SNAPSHOT_ID, OLD_SHA)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        runtime_sync,
        "fetch_optional_trusted_branch_head",
        lambda *a, **k: _head(OTHER_SHA),
    )
    monkeypatch.setattr(
        runtime_sync,
        "anchor_trusted_baseline",
        lambda *a, **k: pytest.fail("diverged runtime branch must not be anchored"),
    )
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is runtime_sync.RuntimeSyncDisposition.DIVERGED


def test_missing_runtime_branch_after_baseline_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "runtime", SNAPSHOT_ID, OLD_SHA)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        runtime_sync,
        "fetch_optional_trusted_branch_head",
        lambda *a, **k: _absence(),
    )
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is runtime_sync.RuntimeSyncDisposition.REMOTE_MISSING


def test_first_runtime_publication_uses_verified_publication_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        runtime_sync,
        "fetch_optional_trusted_branch_head",
        lambda *a, **k: _absence(),
    )
    monkeypatch.setattr(runtime_sync, "create_snapshot_commit", lambda workspace: NEW_SHA)
    intent = PublicationIntent(TARGET, REPOSITORY_ID, "runtime", NEW_SHA, None, True)
    monkeypatch.setattr(
        runtime_sync,
        "build_publication_intent",
        lambda workspace, preflight: intent,
    )

    def complete(
        state: StateStore,
        workspace: GitWorkspace,
        supplied: PublicationIntent,
        token: str,
    ) -> SynchronizationBaseline:
        assert supplied == intent
        assert token == "token"
        return state.record_synchronization_baseline(
            TARGET,
            "runtime",
            workspace.snapshot_id,
            NEW_SHA,
        )

    monkeypatch.setattr(runtime_sync, "complete_authorized_publication", complete)
    try:
        result = _run(store, tmp_path)
        persisted = store.synchronization_baseline(TARGET, "runtime")
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is runtime_sync.RuntimeSyncDisposition.INITIALIZED
    assert result.commit_sha == NEW_SHA
    assert persisted is not None and persisted.commit_sha == NEW_SHA


def test_existing_runtime_history_publishes_only_after_exact_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "runtime", SNAPSHOT_ID, OLD_SHA)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(runtime_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _head())

    def anchor(workspace: GitWorkspace, remote: BranchHead, token: str) -> str:
        calls.append(("anchor", remote.commit_sha))
        return remote.commit_sha

    def commit(workspace: GitWorkspace) -> str:
        calls.append(("commit", workspace.snapshot_id))
        return NEW_SHA

    monkeypatch.setattr(runtime_sync, "anchor_trusted_baseline", anchor)
    monkeypatch.setattr(runtime_sync, "create_snapshot_commit", commit)
    monkeypatch.setattr(
        runtime_sync,
        "build_publication_intent",
        lambda workspace, preflight: PublicationIntent(
            TARGET,
            REPOSITORY_ID,
            "runtime",
            NEW_SHA,
            OLD_SHA,
            False,
        ),
    )
    monkeypatch.setattr(
        runtime_sync,
        "complete_authorized_publication",
        lambda state, workspace, intent, token: state.record_synchronization_baseline(
            TARGET,
            "runtime",
            workspace.snapshot_id,
            NEW_SHA,
        ),
    )
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is runtime_sync.RuntimeSyncDisposition.PUBLISHED
    names = [name for name, _ in calls]
    assert names.index("anchor") < names.index("commit")


def test_no_change_runtime_inventory_does_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    baseline = store.record_synchronization_baseline(TARGET, "runtime", SNAPSHOT_ID, OLD_SHA)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(runtime_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _head())
    monkeypatch.setattr(runtime_sync, "anchor_trusted_baseline", lambda *a, **k: OLD_SHA)
    monkeypatch.setattr(runtime_sync, "create_snapshot_commit", lambda workspace: None)
    monkeypatch.setattr(
        runtime_sync,
        "complete_authorized_publication",
        lambda *a, **k: pytest.fail("no-change runtime inventory must not publish"),
    )
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is runtime_sync.RuntimeSyncDisposition.NO_CHANGE
    assert result.commit_sha == OLD_SHA
    assert result.baseline == baseline


def test_failure_cleans_all_runtime_staging_layers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_staging, snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        runtime_sync,
        "initialize_repository",
        lambda *a, **k: (_ for _ in ()).throw(GitError("expected test failure")),
    )
    try:
        with pytest.raises(runtime_sync.RuntimeSyncError, match="failed closed"):
            runtime_sync.synchronize_runtime_inventory(
                store,
                _inventory(),
                runtime_staging,
                snapshots,
                workspaces,
                TARGET,
                "token",
            )
    finally:
        store.__exit__(None, None, None)

    assert list(runtime_staging.iterdir()) == []
    assert list(snapshots.iterdir()) == []
    assert list(workspaces.iterdir()) == []


def test_runtime_inventory_failure_is_sanitized_and_never_enters_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_staging, snapshots, workspaces = _paths(tmp_path)
    secret_detail = "runtime-source-secret-detail"

    def fail(*args: object, **kwargs: object) -> RuntimeInventoryArtifact:
        from ha_syncapp.runtime_inventory import RuntimeInventoryError

        raise RuntimeInventoryError(secret_detail)

    monkeypatch.setattr(runtime_sync, "build_runtime_inventory", fail)
    monkeypatch.setattr(
        runtime_sync,
        "prepare_git_workspace",
        lambda *a, **k: pytest.fail("failed runtime artifact must not reach Git"),
    )
    try:
        with pytest.raises(runtime_sync.RuntimeSyncError, match="failed closed") as error:
            runtime_sync.synchronize_runtime_inventory(
                store,
                _inventory(),
                runtime_staging,
                snapshots,
                workspaces,
                TARGET,
                "token",
            )
    finally:
        store.__exit__(None, None, None)

    assert secret_detail not in str(error.value)
