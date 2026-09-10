"""Materialize one already-fetched candidate commit into protected isolated staging."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import candidate_fetch as fetch_module
from .candidate_fetch import CandidateFetch

_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STAGE_PREFIX = ".git-workspace-candidate-stage-"
_FETCH_REF = "refs/syncapp/candidate-fetch"
_ALLOWED_MODES = {"100644", "100755"}
_MANIFEST_VERSION = 1


class CandidateStageError(RuntimeError):
    """The fetched candidate could not be materialized safely."""


@dataclass(frozen=True, slots=True)
class CandidateStageEntry:
    """Integrity evidence for one materialized Git blob."""

    path: str
    git_mode: str
    object_id: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CandidateStage:
    """Immutable evidence for one verified candidate staging tree."""

    root: Path
    tree: Path
    manifest: Path
    manifest_sha256: str
    target: str
    repository_id: int
    branch: str
    commit_sha: str
    entries: tuple[CandidateStageEntry, ...]


def stage_fetched_candidate(
    fetched: CandidateFetch,
    staging_root: Path,
    home_assistant_root: Path,
) -> CandidateStage:
    """Materialize only the exact fetched commit into staging outside live HA."""
    _validate_fetch_evidence(fetched)
    parent = _trusted_staging_root(staging_root, home_assistant_root, fetched.root)
    _reprove_fetch(fetched)

    root = parent / f"{_STAGE_PREFIX}{uuid.uuid4().hex}.tmp"
    tree = root / "tree"
    manifest = root / "manifest.json"
    accepted = False
    try:
        root.mkdir(mode=0o700)
        tree.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        os.chmod(tree, 0o700)
        raw_tree = _run_git_bytes(
            fetched.root,
            ("ls-tree", "-rz", "--full-tree", fetched.git_ref),
        )
        tree_entries = _parse_tree(raw_tree)
        entries = tuple(
            _materialize_entry(fetched.root, tree, entry) for entry in tree_entries
        )
        manifest_bytes = _manifest_bytes(
            target=fetched.target,
            repository_id=fetched.repository_id,
            branch=fetched.branch,
            commit_sha=fetched.commit_sha,
            entries=entries,
        )
        _write_private_file(manifest, manifest_bytes, executable=False)
        result = CandidateStage(
            root=root,
            tree=tree,
            manifest=manifest,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            target=fetched.target,
            repository_id=fetched.repository_id,
            branch=fetched.branch,
            commit_sha=fetched.commit_sha,
            entries=entries,
        )
        verify_candidate_stage(result)
        _reprove_fetch(fetched)
        accepted = True
        return result
    except CandidateStageError:
        raise
    except OSError as exc:
        raise CandidateStageError("candidate staging filesystem operation failed") from exc
    finally:
        if not accepted:
            shutil.rmtree(root, ignore_errors=True)


def verify_candidate_stage(stage: CandidateStage) -> None:
    """Re-bind returned evidence to the staged bytes before later validation uses it."""
    _validate_stage_evidence(stage)
    _validate_stage_root(stage)
    expected_manifest = _manifest_bytes(
        target=stage.target,
        repository_id=stage.repository_id,
        branch=stage.branch,
        commit_sha=stage.commit_sha,
        entries=stage.entries,
    )
    actual_manifest = _read_private_file(stage.manifest, expected_mode=0o600)
    if actual_manifest != expected_manifest:
        raise CandidateStageError("candidate staging manifest does not match evidence")
    if hashlib.sha256(actual_manifest).hexdigest() != stage.manifest_sha256:
        raise CandidateStageError("candidate staging manifest digest does not match evidence")
    _verify_staged_tree(stage.tree, stage.entries)


def _validate_fetch_evidence(fetched: CandidateFetch) -> None:
    if type(fetched) is not CandidateFetch:
        raise CandidateStageError("candidate fetch evidence is invalid")
    if (
        not isinstance(fetched.target, str)
        or not fetched.target
        or type(fetched.repository_id) is not int
        or fetched.repository_id <= 0
        or fetched.branch != "candidate"
        or not isinstance(fetched.commit_sha, str)
        or _OBJECT_ID.fullmatch(fetched.commit_sha) is None
        or fetched.git_ref != _FETCH_REF
        or not isinstance(fetched.root, Path)
    ):
        raise CandidateStageError("candidate fetch evidence is invalid")


def _validate_stage_evidence(stage: CandidateStage) -> None:
    if type(stage) is not CandidateStage:
        raise CandidateStageError("candidate staging evidence is invalid")
    if (
        not isinstance(stage.root, Path)
        or not isinstance(stage.tree, Path)
        or not isinstance(stage.manifest, Path)
        or not isinstance(stage.target, str)
        or not stage.target
        or type(stage.repository_id) is not int
        or stage.repository_id <= 0
        or stage.branch != "candidate"
        or not isinstance(stage.commit_sha, str)
        or _OBJECT_ID.fullmatch(stage.commit_sha) is None
        or not isinstance(stage.manifest_sha256, str)
        or _SHA256.fullmatch(stage.manifest_sha256) is None
        or type(stage.entries) is not tuple
    ):
        raise CandidateStageError("candidate staging evidence is invalid")
    previous: bytes | None = None
    for entry in stage.entries:
        if type(entry) is not CandidateStageEntry:
            raise CandidateStageError("candidate staging evidence is invalid")
        if (
            not isinstance(entry.path, str)
            or entry.git_mode not in _ALLOWED_MODES
            or not isinstance(entry.object_id, str)
            or _OBJECT_ID.fullmatch(entry.object_id) is None
            or type(entry.size) is not int
            or entry.size < 0
            or not isinstance(entry.sha256, str)
            or _SHA256.fullmatch(entry.sha256) is None
        ):
            raise CandidateStageError("candidate staging evidence is invalid")
        _validate_safe_path(entry.path)
        encoded_path = entry.path.encode("utf-8")
        if previous is not None and encoded_path <= previous:
            raise CandidateStageError("candidate staging evidence is not canonically ordered")
        previous = encoded_path


def _trusted_staging_root(path: Path, home_assistant_root: Path, fetch_root: Path) -> Path:
    if not isinstance(path, Path) or not isinstance(home_assistant_root, Path):
        raise CandidateStageError("candidate staging boundary is invalid")
    try:
        metadata = path.lstat()
        parent = path.resolve(strict=True)
        home = home_assistant_root.resolve(strict=True)
        fetched = fetch_root.resolve(strict=True)
    except OSError as exc:
        raise CandidateStageError("candidate staging root is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise CandidateStageError("candidate staging root is unsafe")
    if _overlaps(parent, home):
        raise CandidateStageError("candidate staging overlaps Home Assistant source")
    if parent == fetched or parent in fetched.parents:
        raise CandidateStageError("candidate staging is inside candidate fetch workspace")
    return parent


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _reprove_fetch(fetched: CandidateFetch) -> None:
    try:
        fetch_module._verify_initialized_workspace(fetched.root)
        executable = fetch_module._git_executable()
        commit = fetch_module._run_git(
            executable,
            fetched.root,
            ("rev-parse", "--verify", f"{fetched.git_ref}^{{commit}}"),
        )
        object_type = fetch_module._run_git(
            executable,
            fetched.root,
            ("cat-file", "-t", f"{fetched.git_ref}^{{commit}}"),
        )
    except fetch_module.CandidateFetchError as exc:
        raise CandidateStageError("candidate fetch workspace could not be reverified") from exc
    if commit != fetched.commit_sha or object_type != "commit":
        raise CandidateStageError("candidate fetch workspace no longer matches evidence")


def _run_git_bytes(root: Path, arguments: tuple[str, ...]) -> bytes:
    executable = fetch_module._git_executable()
    try:
        result = subprocess.run(  # nosec B603
            fetch_module._command(executable, arguments),
            cwd=root,
            env=fetch_module._git_environment(executable, root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateStageError("candidate staging Git command could not execute") from exc
    if result.returncode != 0:
        raise CandidateStageError("candidate staging Git command failed")
    return bytes(result.stdout)


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    git_mode: str
    object_id: str


def _parse_tree(raw: bytes) -> tuple[_TreeEntry, ...]:
    if not isinstance(raw, bytes):
        raise CandidateStageError("candidate Git tree output is invalid")
    entries: list[_TreeEntry] = []
    seen: set[str] = set()
    records = raw.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    for record in records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode_raw, type_raw, object_raw = metadata.split(b" ", 2)
            mode = mode_raw.decode("ascii")
            object_type = type_raw.decode("ascii")
            object_id = object_raw.decode("ascii")
            path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise CandidateStageError("candidate Git tree output is malformed") from exc
        _validate_tree_entry(path, mode, object_type, object_id, seen)
        seen.add(path)
        entries.append(_TreeEntry(path=path, git_mode=mode, object_id=object_id))
    entries.sort(key=lambda entry: entry.path.encode("utf-8"))
    return tuple(entries)


def _validate_tree_entry(
    path: str,
    mode: str,
    object_type: str,
    object_id: str,
    seen: set[str],
) -> None:
    _validate_safe_path(path)
    if mode not in _ALLOWED_MODES or object_type != "blob":
        raise CandidateStageError("candidate Git tree contains an unsupported entry")
    if _OBJECT_ID.fullmatch(object_id) is None:
        raise CandidateStageError("candidate Git tree contains an invalid object ID")
    if path in seen:
        raise CandidateStageError("candidate Git tree contains a duplicate path")
    for existing in seen:
        if path.startswith(existing + "/") or existing.startswith(path + "/"):
            raise CandidateStageError("candidate Git tree contains conflicting paths")


def _validate_safe_path(path: str) -> None:
    parts = path.split("/") if isinstance(path, str) else []
    if (
        not path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.casefold() == ".git" for part in parts)
    ):
        raise CandidateStageError("candidate Git tree contains an unsafe path")


def _materialize_entry(root: Path, tree: Path, entry: _TreeEntry) -> CandidateStageEntry:
    data = _run_git_bytes(root, ("cat-file", "blob", entry.object_id))
    destination = tree.joinpath(*entry.path.split("/"))
    _create_private_parents(tree, destination.parent)
    _write_private_file(destination, data, executable=entry.git_mode == "100755")
    return CandidateStageEntry(
        path=entry.path,
        git_mode=entry.git_mode,
        object_id=entry.object_id,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _create_private_parents(tree: Path, parent: Path) -> None:
    relative = parent.relative_to(tree)
    current = tree
    for component in relative.parts:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
                raise CandidateStageError("candidate staging parent is unsafe") from None
        os.chmod(current, 0o700)


def _write_private_file(path: Path, data: bytes, *, executable: bool) -> None:
    mode = 0o700 if executable else 0o600
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
    except OSError as exc:
        raise CandidateStageError("candidate staging file could not be written") from exc


def _manifest_bytes(
    *,
    target: str,
    repository_id: int,
    branch: str,
    commit_sha: str,
    entries: tuple[CandidateStageEntry, ...],
) -> bytes:
    payload = {
        "version": _MANIFEST_VERSION,
        "target": target,
        "repository_id": repository_id,
        "branch": branch,
        "commit_sha": commit_sha,
        "entries": [
            {
                "path": entry.path,
                "git_mode": entry.git_mode,
                "object_id": entry.object_id,
                "size": entry.size,
                "sha256": entry.sha256,
            }
            for entry in entries
        ],
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _validate_stage_root(stage: CandidateStage) -> None:
    try:
        root = stage.root.lstat()
        tree = stage.tree.lstat()
        manifest = stage.manifest.lstat()
    except OSError as exc:
        raise CandidateStageError("candidate staging evidence paths are unavailable") from exc
    if (
        not stat.S_ISDIR(root.st_mode)
        or stage.root.is_symlink()
        or root.st_uid != os.geteuid()
        or stat.S_IMODE(root.st_mode) != 0o700
        or stage.tree.parent != stage.root
        or not stat.S_ISDIR(tree.st_mode)
        or stage.tree.is_symlink()
        or tree.st_uid != os.geteuid()
        or stat.S_IMODE(tree.st_mode) != 0o700
        or stage.manifest.parent != stage.root
        or not stat.S_ISREG(manifest.st_mode)
        or stage.manifest.is_symlink()
        or manifest.st_uid != os.geteuid()
        or manifest.st_nlink != 1
        or stat.S_IMODE(manifest.st_mode) != 0o600
    ):
        raise CandidateStageError("candidate staging evidence paths are unsafe")
    if {entry.name for entry in os.scandir(stage.root)} != {"tree", "manifest.json"}:
        raise CandidateStageError("candidate staging root contains unexpected entries")


def _verify_staged_tree(tree: Path, entries: tuple[CandidateStageEntry, ...]) -> None:
    expected = {entry.path: entry for entry in entries}
    expected_directories = {""}
    for entry in entries:
        parts = entry.path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:index]))
    actual: set[str] = set()
    actual_directories: set[str] = set()
    try:
        for directory, dirnames, filenames in os.walk(tree, followlinks=False):
            directory_path = Path(directory)
            relative_directory = directory_path.relative_to(tree).as_posix()
            if relative_directory == ".":
                relative_directory = ""
            actual_directories.add(relative_directory)
            directory_info = directory_path.lstat()
            if (
                relative_directory not in expected_directories
                or not stat.S_ISDIR(directory_info.st_mode)
                or directory_path.is_symlink()
                or directory_info.st_uid != os.geteuid()
                or stat.S_IMODE(directory_info.st_mode) != 0o700
            ):
                raise CandidateStageError("candidate staging contains an unsafe directory")
            for name in dirnames:
                child = directory_path / name
                child_info = child.lstat()
                if not stat.S_ISDIR(child_info.st_mode) or child.is_symlink():
                    raise CandidateStageError("candidate staging contains an unsafe directory")
            for name in filenames:
                path = directory_path / name
                relative = path.relative_to(tree).as_posix()
                actual.add(relative)
                entry = expected.get(relative)
                if entry is None:
                    raise CandidateStageError("candidate staging contains an unexpected file")
                _verify_staged_file(path, entry)
    except OSError as exc:
        raise CandidateStageError("candidate staging verification failed") from exc
    if actual != set(expected) or actual_directories != expected_directories:
        raise CandidateStageError("candidate staging file set does not match integrity evidence")


def _verify_staged_file(path: Path, entry: CandidateStageEntry) -> None:
    expected_mode = 0o700 if entry.git_mode == "100755" else 0o600
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            digest = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise CandidateStageError("candidate staged file could not be verified") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != expected_mode
        or info.st_size != entry.size
        or digest.hexdigest() != entry.sha256
    ):
        raise CandidateStageError(
            "candidate staged file does not match integrity evidence"
        )


def _read_private_file(path: Path, *, expected_mode: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            data = handle.read()
    except OSError as exc:
        raise CandidateStageError("candidate staging manifest is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != expected_mode
    ):
        raise CandidateStageError("candidate staging manifest is unsafe")
    return data
