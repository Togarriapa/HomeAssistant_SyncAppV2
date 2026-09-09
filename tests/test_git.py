import subprocess
from pathlib import Path

import pytest
from ha_syncapp.errors import Failure
from ha_syncapp.files import Snapshot
from ha_syncapp.git import BRANCHES, GitRepository


def remote_repo(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    return str(remote)


def test_raw_git_fidelity_and_ref_leases(tmp_path: Path) -> None:
    remote = remote_repo(tmp_path)
    repo = GitRepository(tmp_path / "stage.git", remote)
    files = {
        "configuration.yaml": b"a:\r\n",
        ".storage/auth": b"\x00\xff\n",
        ".gitattributes": b"* text eol=lf\n",
    }
    snap = Snapshot(files)
    commit = repo.commit(snap, parent=None, message="one", timestamp=100)
    repo.publish("main", commit, expected=None)
    assert repo.ref("main") == commit
    assert repo.snapshot(commit).files == files
    repo.publish("main", commit, expected=None)  # Lost acknowledgement is safe to replay.
    second = repo.commit(
        Snapshot({**files, "x": b"2"}), parent=commit, message="two", timestamp=101
    )
    repo.publish("main", second, expected=commit)
    with pytest.raises(Failure, match="remote_conflict"):
        repo.publish("main", commit, expected=commit)
    assert repo.ref("main") == second
    assert repo.is_ancestor(commit, second)
    repo.tag("known-good/test", second)
    repo.tag("known-good/test", second)


def test_generated_branches_have_bounded_history(tmp_path: Path) -> None:
    repo = GitRepository(tmp_path / "stage.git", remote_repo(tmp_path))
    first = repo.commit(
        Snapshot({"logs/today.json": b"1"}), parent=None, message="logs", timestamp=1
    )
    repo.publish("logs", first, expected=None, generated=True)
    second = repo.commit(
        Snapshot({"logs/today.json": b"2"}), parent=None, message="logs", timestamp=2
    )
    repo.publish("logs", second, expected=first, generated=True)
    assert repo.run("rev-list", "--count", second).strip() == b"1"
    with pytest.raises(Failure):
        repo.publish("main", second, expected=first, generated=True)


def test_remote_data_cannot_introduce_symlinks(tmp_path: Path) -> None:
    repo = GitRepository(tmp_path / "stage.git", remote_repo(tmp_path))
    blob = repo.run("hash-object", "-w", "--stdin", data=b"/etc/passwd").strip()
    tree = (
        repo.run("mktree", data=b"120000 blob " + blob + b"\tconfiguration.yaml\n").strip().decode()
    )
    commit = repo.run("commit-tree", tree, data=b"unsafe").strip().decode()
    with pytest.raises(Failure):
        repo.snapshot(commit)


def test_initialization_is_atomic_idempotent_and_requires_empty_remote(tmp_path: Path) -> None:
    repo = GitRepository(tmp_path / "stage.git", remote_repo(tmp_path))
    commit = repo.commit(
        Snapshot({"configuration.yaml": b"default_config:\n"}),
        parent=None,
        message="initialize",
        timestamp=1,
    )
    planned = dict.fromkeys(BRANCHES, commit)
    repo.test_access("1" * 32)
    assert repo.remote_refs() == {}
    repo.initialize(planned)
    repo.initialize(planned)  # A lost acknowledgement must not duplicate initial history.
    assert repo.remote_refs() == {f"refs/heads/{b}": commit for b in BRANCHES}
    changed = repo.commit(
        Snapshot({"configuration.yaml": b"changed"}),
        parent=commit,
        message="other writer",
        timestamp=2,
    )
    repo.publish("candidate", changed, expected=commit)
    with pytest.raises(Failure, match="repository_not_empty"):
        repo.initialize(planned)
    assert repo.ref("main") == commit


def test_https_cannot_be_used_for_git_content(tmp_path: Path) -> None:
    with pytest.raises(Failure, match="unsupported_git_remote"):
        GitRepository(tmp_path / "stage.git", "https://github.com/owner/repo.git")


def test_missing_ssh_identity_or_guard_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(Failure):
        GitRepository(tmp_path / "stage.git", "git@github.com:owner/repo.git")
