from pathlib import Path

import ha_syncapp.publication_transport as transport_module
import pytest
from ha_syncapp.git_workspace import GitWorkspace, prepare_git_workspace
from ha_syncapp.github_repo import BranchAbsence, BranchHead
from ha_syncapp.local_git import GitError, create_snapshot_commit, initialize_repository
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.publication_transport import PublicationTransportError, push_publication_intent
from ha_syncapp.snapshot import capture_snapshot

TOKEN = "github-token-value"
BASELINE = "a" * 40


def _workspace(tmp_path: Path) -> tuple[GitWorkspace, str]:
    source = tmp_path / "ha"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    source.mkdir()
    snapshots.mkdir()
    workspaces.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    snapshot = capture_snapshot(source, snapshots)
    workspace = prepare_git_workspace(snapshot.root, workspaces)
    initialize_repository(workspace)
    commit_sha = create_snapshot_commit(workspace)
    assert commit_sha is not None
    return workspace, commit_sha


def _normal_intent(commit_sha: str) -> PublicationIntent:
    return PublicationIntent(
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=commit_sha,
        expected_remote_commit_sha=BASELINE,
        expect_remote_absent=False,
    )


def _initial_intent(commit_sha: str) -> PublicationIntent:
    return PublicationIntent(
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=commit_sha,
        expected_remote_commit_sha=None,
        expect_remote_absent=True,
    )


def test_normal_publication_uses_exact_non_force_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    intent = _normal_intent(commit_sha)
    remote = BranchHead("Owner/Home", 42, "main", BASELINE)
    calls: list[tuple[str, ...]] = []

    def fake_run(
        _executable: str,
        _tree: Path,
        _root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        calls.append(arguments)
        if arguments[0] == "rev-parse":
            return commit_sha
        return "ok"

    monkeypatch.setattr(transport_module, "_run_git", fake_run)

    published = push_publication_intent(workspace, intent, remote, TOKEN)

    assert published == commit_sha
    push = next(arguments for arguments in calls if arguments[0] == "push")
    assert push == (
        "push",
        "--porcelain",
        "--no-verify",
        "https://github.com/Owner/Home.git",
        f"{commit_sha}:refs/heads/main",
    )
    assert not any(argument.startswith("--force") for argument in push)
    assert all(TOKEN not in argument for arguments in calls for argument in arguments)
    assert not (workspace.root / ".syncapp-push-askpass").exists()


def test_first_publication_requires_fresh_identity_bound_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    intent = _initial_intent(commit_sha)
    remote = BranchAbsence("Owner/Home", 42, "main")

    monkeypatch.setattr(
        transport_module,
        "_run_git",
        lambda _executable, _tree, _root, arguments, **_kwargs: (
            commit_sha if arguments[0] == "rev-parse" else "ok"
        ),
    )

    assert push_publication_intent(workspace, intent, remote, TOKEN) == commit_sha


def test_stale_normal_remote_is_rejected_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    intent = _normal_intent(commit_sha)
    remote = BranchHead("Owner/Home", 42, "main", "b" * 40)
    called = False

    def unexpected(*_args: object, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(transport_module, "_run_git", unexpected)

    with pytest.raises(PublicationTransportError, match="changed after authorization"):
        push_publication_intent(workspace, intent, remote, TOKEN)

    assert not called


def test_initial_publication_is_rejected_when_branch_now_exists(tmp_path: Path) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    intent = _initial_intent(commit_sha)
    remote = BranchHead("Owner/Home", 42, "main", BASELINE)

    with pytest.raises(PublicationTransportError, match="no longer absent"):
        push_publication_intent(workspace, intent, remote, TOKEN)


def test_authentication_helper_contains_no_token(tmp_path: Path) -> None:
    helper = transport_module._create_askpass(tmp_path)
    try:
        content = helper.read_text()
        environment = transport_module._git_environment(
            "/usr/bin/git", tmp_path, token=TOKEN, askpass=helper
        )
        command = transport_module._command(
            "/usr/bin/git", ("push", "https://github.com/Owner/Home.git")
        )

        assert TOKEN not in content
        assert environment["SYNCAPP_GITHUB_TOKEN"] == TOKEN
        assert environment["GIT_ASKPASS"] == str(helper)
        assert all(TOKEN not in argument for argument in command)
        assert "credential.helper=" in command
    finally:
        helper.unlink(missing_ok=True)


def test_workspace_drift_before_transport_fails_closed(tmp_path: Path) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    intent = _normal_intent(commit_sha)
    remote = BranchHead("Owner/Home", 42, "main", BASELINE)
    (workspace.tree_path / "configuration.yaml").write_text("homeassistant:\n  name: changed\n")

    with pytest.raises(PublicationTransportError, match="could not be re-proven"):
        push_publication_intent(workspace, intent, remote, TOKEN)


def test_workspace_drift_during_rejected_push_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, commit_sha = _workspace(tmp_path)
    intent = _normal_intent(commit_sha)
    remote = BranchHead("Owner/Home", 42, "main", BASELINE)

    def fake_run(
        _executable: str,
        _tree: Path,
        _root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        if arguments[0] == "rev-parse":
            return commit_sha
        (workspace.tree_path / "configuration.yaml").write_text("homeassistant:\n  name: changed\n")
        raise GitError("simulated rejection")

    monkeypatch.setattr(transport_module, "_run_git", fake_run)

    with pytest.raises(PublicationTransportError, match="changed during transport"):
        push_publication_intent(workspace, intent, remote, TOKEN)

    assert not (workspace.root / ".syncapp-push-askpass").exists()


def test_forged_publication_intent_is_rejected_before_git(tmp_path: Path) -> None:
    workspace, _commit_sha = _workspace(tmp_path)
    forged = PublicationIntent(
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha="not-a-sha",
        expected_remote_commit_sha=BASELINE,
        expect_remote_absent=False,
    )
    remote = BranchHead("Owner/Home", 42, "main", BASELINE)

    with pytest.raises(PublicationTransportError, match="local commit identity"):
        push_publication_intent(workspace, forged, remote, TOKEN)
