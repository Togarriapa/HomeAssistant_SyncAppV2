import os
import stat
from pathlib import Path

import pytest
from ha_syncapp.scratch import ScratchError, cleanup_stale_sync_scratch, prepare_sync_scratch


def _state_root(tmp_path: Path) -> Path:
    root = tmp_path / "syncapp"
    root.mkdir(mode=0o700)
    return root


def test_prepare_creates_owner_only_scratch_roots(tmp_path: Path) -> None:
    root = _state_root(tmp_path)

    scratch = prepare_sync_scratch(root)

    assert scratch.snapshots == root / "snapshots"
    assert scratch.workspaces == root / "workspaces"
    for path in (scratch.snapshots, scratch.workspaces):
        info = path.lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.geteuid()


def test_prepare_repairs_directory_permissions(tmp_path: Path) -> None:
    root = _state_root(tmp_path)
    snapshots = root / "snapshots"
    workspaces = root / "workspaces"
    snapshots.mkdir(mode=0o755)
    workspaces.mkdir(mode=0o777)
    snapshots.chmod(0o755)
    workspaces.chmod(0o777)

    prepare_sync_scratch(root)

    assert stat.S_IMODE(snapshots.stat().st_mode) == 0o700
    assert stat.S_IMODE(workspaces.stat().st_mode) == 0o700


def test_prepare_rejects_symlinked_root(tmp_path: Path) -> None:
    real = _state_root(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(ScratchError, match="safe directory"):
        prepare_sync_scratch(alias)


def test_prepare_rejects_symlinked_scratch_child(tmp_path: Path) -> None:
    root = _state_root(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "snapshots").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises((ScratchError, OSError)):
        prepare_sync_scratch(root)


def test_cleanup_removes_only_recognized_transient_directories(tmp_path: Path) -> None:
    root = _state_root(tmp_path)
    scratch = prepare_sync_scratch(root)
    stale_snapshot = scratch.snapshots / ".snapshot-123.tmp"
    stale_workspace = scratch.workspaces / ".git-workspace-456.tmp"
    for path in (stale_snapshot, stale_workspace):
        nested = path / "nested"
        nested.mkdir(parents=True)
        (nested / "payload").write_bytes(b"payload")
    preserved = scratch.snapshots / "keep-me"
    preserved.mkdir()
    (root / "state.sqlite3").write_bytes(b"durable")
    (root / "instance.lock").write_bytes(b"lock")

    removed = cleanup_stale_sync_scratch(root)

    assert removed == 2
    assert not stale_snapshot.exists()
    assert not stale_workspace.exists()
    assert preserved.is_dir()
    assert (root / "state.sqlite3").read_bytes() == b"durable"
    assert (root / "instance.lock").read_bytes() == b"lock"


def test_cleanup_fails_closed_on_symlink_inside_recognized_transient(tmp_path: Path) -> None:
    root = _state_root(tmp_path)
    scratch = prepare_sync_scratch(root)
    stale = scratch.snapshots / ".snapshot-unsafe.tmp"
    stale.mkdir()
    target = tmp_path / "target"
    target.write_text("preserve")
    (stale / "link").symlink_to(target)

    with pytest.raises(ScratchError, match="unsafe file type"):
        cleanup_stale_sync_scratch(root)

    assert target.read_text() == "preserve"
    assert stale.exists()


def test_cleanup_ignores_similarly_named_non_transient_entries(tmp_path: Path) -> None:
    root = _state_root(tmp_path)
    scratch = prepare_sync_scratch(root)
    entries = [
        scratch.snapshots / ".snapshot-not-temp",
        scratch.workspaces / "git-workspace-123.tmp",
        scratch.workspaces / ".git-workspace-123.keep",
    ]
    for entry in entries:
        entry.mkdir()

    assert cleanup_stale_sync_scratch(root) == 0
    assert all(entry.is_dir() for entry in entries)
