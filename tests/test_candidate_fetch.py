from pathlib import Path
from types import SimpleNamespace

import ha_syncapp.candidate_fetch as fetch_module
import pytest
from ha_syncapp.candidate_detection import CandidateObservation
from ha_syncapp.candidate_fetch import CandidateFetchError, fetch_trusted_candidate

SHA = "a" * 40
OTHER_SHA = "b" * 40
TOKEN = "github-token-value"
TARGET = "Owner/Home"
REPOSITORY_ID = 42


def _observation(commit_sha: str = SHA) -> CandidateObservation:
    return CandidateObservation(TARGET, REPOSITORY_ID, "candidate", commit_sha)


def _workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _successful_git(calls: list[tuple[str, ...]]):
    def run(
        _executable: str,
        root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        calls.append(arguments)
        if arguments[0] == "init":
            (root / ".git").mkdir()
            return ""
        if arguments[0] == "rev-parse":
            return SHA
        if arguments[0] == "cat-file":
            return "commit"
        return ""

    return run


def test_fetches_only_exact_candidate_ref_without_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(fetch_module, "_run_git", _successful_git(calls))

    result = fetch_trusted_candidate(
        _observation(),
        SHA,
        TOKEN,
        _workspace_root(tmp_path),
    )

    assert result.target == TARGET
    assert result.repository_id == REPOSITORY_ID
    assert result.branch == "candidate"
    assert result.commit_sha == SHA
    assert result.git_ref == "refs/syncapp/candidate-fetch"
    assert result.root.name.startswith(".git-workspace-candidate-")
    assert result.root.name.endswith(".tmp")
    assert {entry.name for entry in result.root.iterdir()} == {".git"}
    assert not (result.root / ".syncapp-askpass").exists()

    fetch = next(arguments for arguments in calls if arguments[0] == "fetch")
    assert fetch == (
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--depth=1",
        "https://github.com/Owner/Home.git",
        "+refs/heads/candidate:refs/syncapp/candidate-fetch",
    )
    forbidden = {"checkout", "merge", "reset", "pull", "push", "switch"}
    assert not forbidden.intersection(arguments[0] for arguments in calls)
    assert all(TOKEN not in argument for arguments in calls for argument in arguments)


def test_candidate_fetch_authentication_is_not_in_command_or_url(tmp_path: Path) -> None:
    root = _workspace_root(tmp_path)
    helper = root / "askpass"
    environment = fetch_module._git_environment(
        "/usr/bin/git",
        root,
        token=TOKEN,
        askpass=helper,
    )
    command = fetch_module._command(
        "/usr/bin/git",
        ("fetch", "https://github.com/Owner/Home.git"),
    )

    assert environment["SYNCAPP_GITHUB_TOKEN"] == TOKEN
    assert environment["GIT_ASKPASS"] == str(helper)
    assert all(TOKEN not in argument for argument in command)
    assert "credential.helper=" in command
    assert fetch_module._repository_url(TARGET) == "https://github.com/Owner/Home.git"
    assert TOKEN not in fetch_module._repository_url(TARGET)


def test_branch_movement_or_sha_mismatch_removes_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces = _workspace_root(tmp_path)

    def run(
        _executable: str,
        root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        if arguments[0] == "init":
            (root / ".git").mkdir()
        if arguments[0] == "rev-parse":
            return OTHER_SHA
        if arguments[0] == "cat-file":
            return "commit"
        return ""

    monkeypatch.setattr(fetch_module, "_run_git", run)
    with pytest.raises(CandidateFetchError, match="does not match trusted observation"):
        fetch_trusted_candidate(_observation(), SHA, TOKEN, workspaces)

    assert list(workspaces.iterdir()) == []


@pytest.mark.parametrize(
    ("observation", "expected_sha", "message"),
    [
        (CandidateObservation(TARGET, REPOSITORY_ID, "main", SHA), SHA, "branch is invalid"),
        (CandidateObservation(TARGET, 0, "candidate", SHA), SHA, "identity is invalid"),
        (CandidateObservation(TARGET, REPOSITORY_ID, "candidate", None), SHA, "commit is invalid"),
        (CandidateObservation(TARGET, REPOSITORY_ID, "candidate", SHA), "bad", "expected"),
        (CandidateObservation(TARGET, REPOSITORY_ID, "candidate", SHA), OTHER_SHA, "does not match"),
    ],
)
def test_invalid_candidate_evidence_fails_before_workspace_creation(
    tmp_path: Path,
    observation: CandidateObservation,
    expected_sha: str,
    message: str,
) -> None:
    workspaces = _workspace_root(tmp_path)
    with pytest.raises(CandidateFetchError, match=message):
        fetch_trusted_candidate(observation, expected_sha, TOKEN, workspaces)
    assert list(workspaces.iterdir()) == []


def test_unsafe_workspace_root_is_rejected(tmp_path: Path) -> None:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir(mode=0o755)
    workspaces.chmod(0o755)

    with pytest.raises(CandidateFetchError, match="root is unsafe"):
        fetch_trusted_candidate(_observation(), SHA, TOKEN, workspaces)
    assert list(workspaces.iterdir()) == []


def test_git_failure_removes_partial_workspace_and_auth_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces = _workspace_root(tmp_path)

    def run(
        _executable: str,
        root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        if arguments[0] == "init":
            (root / ".git").mkdir()
            return ""
        raise CandidateFetchError("confined Git command failed")

    monkeypatch.setattr(fetch_module, "_run_git", run)
    with pytest.raises(CandidateFetchError, match="confined Git command failed"):
        fetch_trusted_candidate(_observation(), SHA, TOKEN, workspaces)
    assert list(workspaces.iterdir()) == []


def test_git_transport_failure_does_not_expose_captured_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace_root(tmp_path)

    def failed_run(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"remote rejected {TOKEN}",
        )

    monkeypatch.setattr(fetch_module.subprocess, "run", failed_run)
    with pytest.raises(CandidateFetchError) as caught:
        fetch_module._run_git(
            "/usr/bin/git",
            root,
            ("fetch", "https://github.com/Owner/Home.git"),
            token=TOKEN,
            askpass=root / "askpass",
        )
    assert TOKEN not in str(caught.value)
    assert "remote rejected" not in str(caught.value)


def test_non_commit_object_fails_closed_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces = _workspace_root(tmp_path)
    calls: list[tuple[str, ...]] = []

    def run(
        _executable: str,
        root: Path,
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> str:
        calls.append(arguments)
        if arguments[0] == "init":
            (root / ".git").mkdir()
        if arguments[0] == "rev-parse":
            return SHA
        if arguments[0] == "cat-file":
            return "tree"
        return ""

    monkeypatch.setattr(fetch_module, "_run_git", run)
    with pytest.raises(CandidateFetchError, match="object is not a commit"):
        fetch_trusted_candidate(_observation(), SHA, TOKEN, workspaces)
    assert list(workspaces.iterdir()) == []
