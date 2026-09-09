from pathlib import Path

import pytest
import ha_syncapp.baseline_anchor as anchor_module
from ha_syncapp.baseline_anchor import BaselineAnchorError, anchor_trusted_baseline
from ha_syncapp.git_workspace import GitWorkspace, prepare_git_workspace
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.local_git import initialize_repository
from ha_syncapp.snapshot import capture_snapshot

BASELINE = "a" * 40
TOKEN = "github-token-value"


def _workspace(tmp_path: Path) -> GitWorkspace:
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
    return workspace


def _remote(commit_sha: str = BASELINE) -> BranchHead:
    return BranchHead("Owner/Home", 42, "main", commit_sha)


def test_anchor_fetches_exact_branch_without_exposing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(anchor_module, "_branch_exists", lambda *_args: False)

    def fake_run(
        _executable: str,
        _tree: Path,
        _root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        calls.append(arguments)
        if arguments[0] == "rev-parse":
            return BASELINE
        return ""

    monkeypatch.setattr(anchor_module, "_run_git", fake_run)

    anchored = anchor_trusted_baseline(workspace, _remote(), TOKEN)

    assert anchored == BASELINE
    fetch = next(arguments for arguments in calls if arguments[0] == "fetch")
    assert fetch == (
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--depth=1",
        "https://github.com/Owner/Home.git",
        "+refs/heads/main:refs/syncapp/trusted-baseline",
    )
    assert all(TOKEN not in argument for arguments in calls for argument in arguments)
    assert not (workspace.root / ".syncapp-askpass").exists()


def test_authentication_environment_keeps_token_out_of_command(tmp_path: Path) -> None:
    helper = tmp_path / "askpass"
    environment = anchor_module._git_environment(
        "/usr/bin/git", tmp_path, token=TOKEN, askpass=helper
    )
    command = anchor_module._command(
        "/usr/bin/git", ("fetch", "https://github.com/Owner/Home.git")
    )

    assert environment["SYNCAPP_GITHUB_TOKEN"] == TOKEN
    assert environment["GIT_ASKPASS"] == str(helper)
    assert all(TOKEN not in argument for argument in command)
    assert "credential.helper=" in command


def test_fetched_commit_mismatch_fails_before_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(anchor_module, "_branch_exists", lambda *_args: False)

    def fake_run(
        _executable: str,
        _tree: Path,
        _root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        calls.append(arguments)
        if arguments[0] == "rev-parse":
            return "b" * 40
        return ""

    monkeypatch.setattr(anchor_module, "_run_git", fake_run)

    with pytest.raises(BaselineAnchorError, match="does not match trusted remote head"):
        anchor_trusted_baseline(workspace, _remote(), TOKEN)

    assert not any(arguments[0] == "update-ref" for arguments in calls)


def test_existing_local_branch_is_not_reanchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(anchor_module, "_branch_exists", lambda *_args: True)

    with pytest.raises(BaselineAnchorError, match="already anchored or committed"):
        anchor_trusted_baseline(workspace, _remote(), TOKEN)


def test_workspace_change_during_fetch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(anchor_module, "_branch_exists", lambda *_args: False)
    mutated = False

    def fake_run(
        _executable: str,
        _tree: Path,
        _root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        nonlocal mutated
        if arguments[0] == "fetch":
            (workspace.tree_path / "configuration.yaml").write_text(
                "homeassistant:\n  name: changed\n"
            )
            mutated = True
            return ""
        if arguments[0] == "rev-parse":
            return BASELINE
        return ""

    monkeypatch.setattr(anchor_module, "_run_git", fake_run)

    with pytest.raises(BaselineAnchorError, match="trusted baseline acquisition failed"):
        anchor_trusted_baseline(workspace, _remote(), TOKEN)

    assert mutated


def test_invalid_trusted_evidence_is_rejected_before_git(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    invalid = BranchHead("Owner/Home", 42, "main", "not-a-sha")

    with pytest.raises(BaselineAnchorError, match="commit identity"):
        anchor_trusted_baseline(workspace, invalid, TOKEN)
