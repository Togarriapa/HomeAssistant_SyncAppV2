from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp import log_sync
from ha_syncapp.git_workspace import GitWorkspace
from ha_syncapp.github_repo import BranchAbsence, BranchHead
from ha_syncapp.log_artifact import LogArtifact, LogArtifactError, LogRecord, build_log_artifact
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.snapshot import Snapshot
from ha_syncapp.state import StateStore, SynchronizationBaseline

TARGET = "example/private-home-assistant"
REPOSITORY_ID = 12345
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
OTHER_SHA = "3" * 40
SNAPSHOT_ID = "b" * 64
REFERENCE = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store


def _artifact(tmp_path: Path) -> LogArtifact:
    root = tmp_path / "logs-artifacts"
    root.mkdir(mode=0o700)
    return build_log_artifact(
        root,
        (LogRecord("syncapp", "one", REFERENCE, "message"),),
        reference_time=REFERENCE,
    )


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    snapshots.mkdir()
    workspaces.mkdir()
    return snapshots, workspaces


def _install_staging_fakes(
    monkeypatch: pytest.MonkeyPatch,
    artifact: LogArtifact,
    calls: list[tuple[str, object]],
) -> None:
    real_verify = log_sync.verify_log_artifact

    def verify(supplied: LogArtifact) -> None:
        calls.append(("verify", supplied.artifact_id))
        real_verify(supplied)

    def snapshot_capture(source: Path, root: Path) -> Snapshot:
        calls.append(("snapshot_capture", source))
        assert source == artifact.root
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
        assert default_branch == "logs"
        return object()

    monkeypatch.setattr(log_sync, "verify_log_artifact", verify)
    monkeypatch.setattr(log_sync, "capture_snapshot", snapshot_capture)
    monkeypatch.setattr(log_sync, "prepare_git_workspace", prepare)
    monkeypatch.setattr(log_sync, "initialize_repository", initialize)


def _head(commit_sha: str = OLD_SHA) -> BranchHead:
    return BranchHead(TARGET, REPOSITORY_ID, "logs", commit_sha)


def _absence() -> BranchAbsence:
    return BranchAbsence(TARGET, REPOSITORY_ID, "logs")


def _run(
    store: StateStore,
    artifact: LogArtifact,
    tmp_path: Path,
) -> log_sync.LogSyncResult:
    snapshots, workspaces = _paths(tmp_path)
    return log_sync.synchronize_log_artifact(
        store,
        artifact,
        snapshots,
        workspaces,
        TARGET,
        "token",
    )


def test_existing_logs_branch_without_baseline_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    artifact = _artifact(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, artifact, calls)
    monkeypatch.setattr(log_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _head())
    monkeypatch.setattr(
        log_sync,
        "create_snapshot_commit",
        lambda workspace: pytest.fail("baseline-required logs branch must not commit"),
    )
    try:
        result = _run(store, artifact, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is log_sync.LogSyncDisposition.BASELINE_REQUIRED
    assert result.artifact_id == artifact.artifact_id
    assert result.commit_sha is None


def test_diverged_logs_branch_is_blocked_before_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "logs", SNAPSHOT_ID, OLD_SHA)
    artifact = _artifact(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, artifact, calls)
    monkeypatch.setattr(
        log_sync,
        "fetch_optional_trusted_branch_head",
        lambda *a, **k: _head(OTHER_SHA),
    )
    monkeypatch.setattr(
        log_sync,
        "anchor_trusted_baseline",
        lambda *a, **k: pytest.fail("diverged logs branch must not be anchored"),
    )
    try:
        result = _run(store, artifact, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is log_sync.LogSyncDisposition.DIVERGED


def test_missing_logs_branch_after_baseline_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "logs", SNAPSHOT_ID, OLD_SHA)
    artifact = _artifact(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, artifact, calls)
    monkeypatch.setattr(
        log_sync,
        "fetch_optional_trusted_branch_head",
        lambda *a, **k: _absence(),
    )
    try:
        result = _run(store, artifact, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is log_sync.LogSyncDisposition.REMOTE_MISSING


def test_first_logs_publication_uses_verified_publication_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    artifact = _artifact(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, artifact, calls)
    monkeypatch.setattr(
        log_sync,
        "fetch_optional_trusted_branch_head",
        lambda *a, **k: _absence(),
    )
    monkeypatch.setattr(log_sync, "create_snapshot_commit", lambda workspace: NEW_SHA)
    intent = PublicationIntent(TARGET, REPOSITORY_ID, "logs", NEW_SHA, None, True)
    monkeypatch.setattr(
        log_sync,
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
            "logs",
            workspace.snapshot_id,
            NEW_SHA,
        )

    monkeypatch.setattr(log_sync, "complete_authorized_publication", complete)
    try:
        result = _run(store, artifact, tmp_path)
        persisted = store.synchronization_baseline(TARGET, "logs")
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is log_sync.LogSyncDisposition.INITIALIZED
    assert result.commit_sha == NEW_SHA
    assert persisted is not None and persisted.commit_sha == NEW_SHA


def test_existing_logs_history_anchors_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.record_synchronization_baseline(TARGET, "logs", SNAPSHOT_ID, OLD_SHA)
    artifact = _artifact(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, artifact, calls)
    monkeypatch.setattr(log_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _head())

    def anchor(workspace: GitWorkspace, remote: BranchHead, token: str) -> str:
        calls.append(("anchor", remote.commit_sha))
        return remote.commit_sha

    def commit(workspace: GitWorkspace) -> str:
        calls.append(("commit", workspace.snapshot_id))
        return NEW_SHA

    monkeypatch.setattr(log_sync, "anchor_trusted_baseline", anchor)
    monkeypatch.setattr(log_sync, "create_snapshot_commit", commit)
    monkeypatch.setattr(
        log_sync,
        "build_publication_intent",
        lambda workspace, preflight: PublicationIntent(
            TARGET,
            REPOSITORY_ID,
            "logs",
            NEW_SHA,
            OLD_SHA,
            False,
        ),
    )
    monkeypatch.setattr(
        log_sync,
        "complete_authorized_publication",
        lambda state, workspace, intent, token: state.record_synchronization_baseline(
            TARGET,
            "logs",
            workspace.snapshot_id,
            NEW_SHA,
        ),
    )
    try:
        result = _run(store, artifact, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is log_sync.LogSyncDisposition.PUBLISHED
    names = [name for name, _ in calls]
    assert names.index("anchor") < names.index("commit")


def test_no_change_log_artifact_does_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    baseline = store.record_synchronization_baseline(TARGET, "logs", SNAPSHOT_ID, OLD_SHA)
    artifact = _artifact(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, artifact, calls)
    monkeypatch.setattr(log_sync, "fetch_optional_trusted_branch_head", lambda *a, **k: _head())
    monkeypatch.setattr(log_sync, "anchor_trusted_baseline", lambda *a, **k: OLD_SHA)
    monkeypatch.setattr(log_sync, "create_snapshot_commit", lambda workspace: None)
    monkeypatch.setattr(
        log_sync,
        "complete_authorized_publication",
        lambda *a, **k: pytest.fail("no-change logs artifact must not publish"),
    )
    try:
        result = _run(store, artifact, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert result.disposition is log_sync.LogSyncDisposition.NO_CHANGE
    assert result.commit_sha == OLD_SHA
    assert result.baseline == baseline


def test_invalid_log_artifact_never_enters_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    artifact = _artifact(tmp_path)
    (artifact.root / "logs/syncapp/records.jsonl").write_text("tampered\n")
    snapshots, workspaces = _paths(tmp_path)
    monkeypatch.setattr(
        log_sync,
        "capture_snapshot",
        lambda *a, **k: pytest.fail("invalid log artifact must not reach snapshot/Git"),
    )
    try:
        with pytest.raises(log_sync.LogSyncError, match="failed closed") as error:
            log_sync.synchronize_log_artifact(
                store,
                artifact,
                snapshots,
                workspaces,
                TARGET,
                "token",
            )
    finally:
        store.__exit__(None, None, None)

    assert "tampered" not in str(error.value)


def test_failure_cleans_temporary_layers_but_preserves_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    artifact = _artifact(tmp_path)
    snapshots, workspaces = _paths(tmp_path)
    calls: list[tuple[str, object]] = []
    _install_staging_fakes(monkeypatch, artifact, calls)
    monkeypatch.setattr(
        log_sync,
        "fetch_optional_trusted_branch_head",
        lambda *a, **k: (_ for _ in ()).throw(LogArtifactError("secret-detail")),
    )
    try:
        with pytest.raises(log_sync.LogSyncError, match="failed closed") as error:
            log_sync.synchronize_log_artifact(
                store,
                artifact,
                snapshots,
                workspaces,
                TARGET,
                "token",
            )
    finally:
        store.__exit__(None, None, None)

    assert "secret-detail" not in str(error.value)
    assert artifact.root.exists()
    assert list(snapshots.iterdir()) == []
    assert list(workspaces.iterdir()) == []
