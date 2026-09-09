from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp.git_workspace import GitWorkspace, prepare_git_workspace
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.publication_preflight import (
    PublicationDisposition,
    PublicationPreflight,
)
from ha_syncapp.publication_recovery import (
    PublicationRecoveryError,
    build_publication_recovery_intent,
    complete_publication_recovery,
)
from ha_syncapp.snapshot import capture_snapshot
from ha_syncapp.state import StateStore

LOCAL = "b" * 40
PRIOR = "a" * 40
WHEN = datetime(2026, 9, 9, 20, 40, tzinfo=UTC)


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


def _preflight(
    *, disposition: PublicationDisposition = PublicationDisposition.ALREADY_PUBLISHED
) -> PublicationPreflight:
    return PublicationPreflight(
        disposition=disposition,
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=LOCAL,
        remote_commit_sha=LOCAL,
        baseline_commit_sha=PRIOR,
    )


def _remote(commit_sha: str = LOCAL) -> BranchHead:
    return BranchHead("Owner/Home", 42, "main", commit_sha)


def _seed_prior(store: StateStore) -> None:
    store.record_synchronization_baseline(
        "Owner/Home",
        "main",
        "d" * 64,
        PRIOR,
        synchronized_at=datetime(2026, 9, 9, 20, 0, tzinfo=UTC),
    )


def test_already_published_preflight_builds_non_transport_recovery_intent() -> None:
    intent = build_publication_recovery_intent(_preflight())

    assert intent.target == "Owner/Home"
    assert intent.repository_id == 42
    assert intent.branch == "main"
    assert intent.local_commit_sha == LOCAL
    assert intent.prior_baseline_commit_sha == PRIOR


@pytest.mark.parametrize(
    "disposition",
    [
        PublicationDisposition.SAFE_TO_PUBLISH,
        PublicationDisposition.SAFE_TO_INITIALIZE,
        PublicationDisposition.NO_CHANGE,
        PublicationDisposition.DIVERGED,
        PublicationDisposition.REMOTE_MISSING,
        PublicationDisposition.BASELINE_REQUIRED,
    ],
)
def test_non_recovery_preflight_states_are_rejected(disposition: PublicationDisposition) -> None:
    with pytest.raises(PublicationRecoveryError, match="does not authorize recovery"):
        build_publication_recovery_intent(_preflight(disposition=disposition))


def test_forged_already_published_preflight_is_rejected() -> None:
    forged = PublicationPreflight(
        disposition=PublicationDisposition.ALREADY_PUBLISHED,
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=LOCAL,
        remote_commit_sha="c" * 40,
        baseline_commit_sha=PRIOR,
    )

    with pytest.raises(PublicationRecoveryError, match="remote state"):
        build_publication_recovery_intent(forged)


def test_recovery_records_remote_success_without_another_push(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    intent = build_publication_recovery_intent(_preflight())
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 42)
        _seed_prior(store)

        recovered = complete_publication_recovery(
            store,
            workspace,
            intent,
            _remote(),
            synchronized_at=WHEN,
        )

        assert recovered.snapshot_id == workspace.snapshot_id
        assert recovered.commit_sha == LOCAL
        assert recovered.synchronized_at == WHEN


def test_recovery_replay_is_idempotent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    intent = build_publication_recovery_intent(_preflight())
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 42)
        _seed_prior(store)
        first = complete_publication_recovery(
            store, workspace, intent, _remote(), synchronized_at=WHEN
        )
        second = complete_publication_recovery(store, workspace, intent, _remote())

        assert second == first


def test_fresh_remote_movement_blocks_recovery_and_preserves_prior_baseline(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    intent = build_publication_recovery_intent(_preflight())
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 42)
        _seed_prior(store)

        with pytest.raises(PublicationRecoveryError, match="remote commit changed"):
            complete_publication_recovery(store, workspace, intent, _remote("c" * 40))

        assert store.synchronization_baseline("Owner/Home", "main").commit_sha == PRIOR  # type: ignore[union-attr]


def test_mutated_workspace_blocks_recovery_and_preserves_prior_baseline(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace.tree_path / "configuration.yaml").write_text("homeassistant:\n  name: changed\n")
    intent = build_publication_recovery_intent(_preflight())
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 42)
        _seed_prior(store)

        with pytest.raises(PublicationRecoveryError, match="workspace could not be re-proven"):
            complete_publication_recovery(store, workspace, intent, _remote())

        assert store.synchronization_baseline("Owner/Home", "main").commit_sha == PRIOR  # type: ignore[union-attr]


def test_repository_binding_mismatch_blocks_recovery_even_on_replay(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    intent = build_publication_recovery_intent(_preflight())
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 99)
        _seed_prior(store)

        with pytest.raises(PublicationRecoveryError, match="repository binding changed"):
            complete_publication_recovery(store, workspace, intent, _remote())

        assert store.synchronization_baseline("Owner/Home", "main").commit_sha == PRIOR  # type: ignore[union-attr]
