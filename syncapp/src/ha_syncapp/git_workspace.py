from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from ha_syncapp.snapshot import SnapshotError, SnapshotFile, verify_snapshot

_COPY_CHUNK_SIZE = 1024 * 1024
_MANIFEST_VERSION = 1


class WorkspaceError(RuntimeError):
    """Raised when an isolated Git workspace cannot be prepared safely."""


@dataclass(frozen=True, slots=True)
class GitWorkspace:
    snapshot_id: str
    root: Path
    tree_path: Path


def prepare_git_workspace(snapshot_root: Path, workspace_root: Path) -> GitWorkspace:
    """Copy a verified snapshot into a separate mutable workspace for future Git use."""
    accepted = verify_snapshot(snapshot_root)
    workspace_root = _trusted_workspace_root(workspace_root)
    _reject_overlap(accepted.root, workspace_root)
    if any(".git" in Path(entry.path).parts for entry in accepted.files):
        raise WorkspaceError("snapshot collides with reserved Git metadata path")

    root = workspace_root / f".git-workspace-{uuid.uuid4().hex}.tmp"
    tree_path = root / "tree"
    try:
        tree_path.mkdir(parents=True, mode=0o700)
        for entry in accepted.files:
            source = accepted.tree_path / Path(entry.path)
            destination = tree_path / Path(entry.path)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _copy_snapshot_file(source, destination, entry)

        verified_after = verify_snapshot(accepted.root)
        if verified_after.snapshot_id != accepted.snapshot_id:
            raise SnapshotError("snapshot identity changed during workspace materialization")
        workspace = GitWorkspace(snapshot_id=accepted.snapshot_id, root=root, tree_path=tree_path)
        verify_workspace_content(workspace)
        return workspace
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def verify_workspace_content(workspace: GitWorkspace) -> str:
    """Prove non-Git workspace bytes still reproduce the accepted snapshot identity."""
    _, tree = _validate_workspace_layout(workspace)
    files = _scan_workspace_tree(tree)
    payload = {
        "version": _MANIFEST_VERSION,
        "files": [
            {
                "path": item.path,
                "size": item.size,
                "mode": item.mode,
                "sha256": item.sha256,
            }
            for item in files
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    identity = hashlib.sha256(canonical).hexdigest()
    if identity != workspace.snapshot_id:
        raise WorkspaceError("workspace content does not match accepted snapshot identity")
    return identity


def _validate_workspace_layout(workspace: GitWorkspace) -> tuple[Path, Path]:
    if type(workspace) is not GitWorkspace:
        raise WorkspaceError("workspace must be an isolated GitWorkspace")
    root = _trusted_workspace_root(workspace.root)
    tree = _trusted_workspace_root(workspace.tree_path)
    if (
        tree != root / "tree"
        or not root.name.startswith(".git-workspace-")
        or not root.name.endswith(".tmp")
    ):
        raise WorkspaceError("workspace layout is not recognized")
    return root, tree


def _scan_workspace_tree(root: Path) -> tuple[SnapshotFile, ...]:
    files: list[SnapshotFile] = []

    def walk(directory: Path, relative: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise WorkspaceError("workspace tree cannot be scanned safely") from exc
        for entry in entries:
            child_relative = relative / entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceError("workspace entry changed during scan") from exc
            if not relative.parts and entry.name == ".git":
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise WorkspaceError("workspace Git metadata path is unsafe")
                continue
            if entry.name == ".git":
                raise WorkspaceError("nested Git metadata paths are not accepted")
            if stat.S_ISLNK(metadata.st_mode):
                raise WorkspaceError("symbolic links are not accepted in workspace content")
            if stat.S_ISDIR(metadata.st_mode):
                walk(Path(entry.path), child_relative)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise WorkspaceError("workspace content must contain unique regular files")
            digest = _hash_workspace_file(Path(entry.path))
            current = entry.stat(follow_symlinks=False)
            files.append(
                SnapshotFile(
                    path=child_relative.as_posix(),
                    size=current.st_size,
                    mode=stat.S_IMODE(current.st_mode),
                    sha256=digest,
                )
            )

    walk(root, Path())
    return tuple(files)


def _hash_workspace_file(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceError("workspace file cannot be opened safely") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise WorkspaceError("workspace file is not a unique regular file")
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _metadata_identity(before) != _metadata_identity(after):
            raise WorkspaceError("workspace file changed while it was verified")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _trusted_workspace_root(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise WorkspaceError("workspace root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise WorkspaceError("workspace root must be a real directory")
    return path.resolve(strict=True)


def _reject_overlap(snapshot_root: Path, workspace_root: Path) -> None:
    if (
        snapshot_root == workspace_root
        or snapshot_root in workspace_root.parents
        or workspace_root in snapshot_root.parents
    ):
        raise WorkspaceError("snapshot and workspace roots must not overlap")


def _copy_snapshot_file(source: Path, destination: Path, expected: SnapshotFile) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise SnapshotError("snapshot file cannot be opened safely") from exc

    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        _validate_snapshot_file(before, expected)
        with destination.open("xb") as target:
            while True:
                chunk = os.read(descriptor, _COPY_CHUNK_SIZE)
                if not chunk:
                    break
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(descriptor)
        if _metadata_identity(before) != _metadata_identity(after):
            raise SnapshotError("snapshot file changed during workspace materialization")
        if digest.hexdigest() != expected.sha256:
            raise SnapshotError("snapshot bytes changed during workspace materialization")
        os.chmod(destination, expected.mode)
    finally:
        os.close(descriptor)


def _validate_snapshot_file(metadata: os.stat_result, expected: SnapshotFile) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != expected.size
        or stat.S_IMODE(metadata.st_mode) != expected.mode
    ):
        raise SnapshotError("snapshot file metadata changed before workspace materialization")


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
