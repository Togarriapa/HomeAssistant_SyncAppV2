"""Materialize one fetched candidate commit into verified isolated staging."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess  # nosec B404
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .candidate_fetch import CandidateFetch
from .snapshot import Snapshot, SnapshotError, capture_snapshot, verify_snapshot

_MANIFEST_VERSION: Final = 1
_CANDIDATE_BRANCH: Final = "candidate"
_FETCH_REF: Final = "refs/syncapp/candidate-fetch"
_SHA: Final = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BLOB_MODE: Final = {"100644": 0o644, "100755": 0o755}
_MAX_GIT_METADATA_BYTES: Final = 16 * 1024 * 1024
_DISK_RESERVE_BYTES: Final = 64 * 1024 * 1024


class CandidateStageError(RuntimeError):
    """Candidate staging could not be completed or verified safely."""


@dataclass(frozen=True, slots=True)
class CandidateTreeFile:
    """One regular blob selected from the exact fetched commit tree."""

    path: str
    blob_oid: str
    size: int
    mode: int


@dataclass(frozen=True, slots=True)
class CandidateStage:
    """Integrity-bound evidence for one isolated candidate staging tree."""

    stage_id: str
    root: Path
    tree_path: Path
    manifest_path: Path
    target: str
    repository_id: int
    branch: str
    commit_sha: str
    snapshot_id: str


def stage_fetched_candidate(
    fetched: CandidateFetch,
    staging_root: Path,
    home_assistant_root: Path,
) -> CandidateStage:
    """Materialize only regular blobs from one re-verified fetched candidate commit."""
    fetched = _verify_fetched_candidate(fetched)
    parent = _trusted_staging_root(staging_root, home_assistant_root, fetched.root)
    snapshot_parent = _private_subdirectory(parent / ".candidate-snapshots")
    source = parent / f".candidate-source-{uuid.uuid4().hex}.tmp"
    snapshot: Snapshot | None = None
    accepted = False
    try:
        source.mkdir(mode=0o700)
        os.chmod(source, 0o700)
        files = _list_candidate_tree(fetched)
        required = sum(item.size for item in files)
        free = shutil.disk_usage(parent).free
        if required > max(0, free - _DISK_RESERVE_BYTES):
            raise CandidateStageError("candidate staging has insufficient free space")
        for item in files:
            destination = source.joinpath(*item.path.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _materialize_blob(fetched.root, item, destination)

        snapshot = capture_snapshot(source, snapshot_parent)
        os.chmod(snapshot.root, 0o700)
        manifest_path = snapshot.root / "candidate.json"
        manifest_path.write_bytes(_candidate_manifest_bytes(fetched, snapshot))
        os.chmod(manifest_path, 0o600)
        stage = verify_candidate_stage(snapshot.root)
        if (
            stage.target != fetched.target
            or stage.repository_id != fetched.repository_id
            or stage.commit_sha != fetched.commit_sha
        ):
            raise CandidateStageError("candidate staging identity changed during verification")
        accepted = True
        return stage
    except CandidateStageError:
        raise
    except SnapshotError as exc:
        raise CandidateStageError("candidate staging snapshot failed") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateStageError("candidate staging failed") from exc
    finally:
        shutil.rmtree(source, ignore_errors=True)
        if snapshot is not None and not accepted:
            shutil.rmtree(snapshot.root, ignore_errors=True)


def verify_candidate_stage(root: Path) -> CandidateStage:
    """Re-bind staged candidate bytes to their canonical candidate manifest."""
    root = _trusted_candidate_stage_root(root)
    try:
        entries = {entry.name for entry in os.scandir(root)}
    except OSError as exc:
        raise CandidateStageError("candidate stage cannot be inspected") from exc
    if entries != {"tree", "manifest.json", "candidate.json"}:
        raise CandidateStageError("candidate stage contains unexpected root entries")

    try:
        snapshot = verify_snapshot(root)
    except SnapshotError as exc:
        raise CandidateStageError("candidate stage snapshot verification failed") from exc

    manifest_path = root / "candidate.json"
    document, raw = _read_candidate_manifest(manifest_path)
    expected_keys = {
        "version",
        "stage_id",
        "target",
        "repository_id",
        "branch",
        "commit_sha",
        "snapshot_id",
    }
    if set(document) != expected_keys or document.get("version") != _MANIFEST_VERSION:
        raise CandidateStageError("candidate stage manifest schema is invalid")

    target = document.get("target")
    repository_id = document.get("repository_id")
    branch = document.get("branch")
    commit_sha = document.get("commit_sha")
    snapshot_id = document.get("snapshot_id")
    stage_id = document.get("stage_id")
    if (
        not isinstance(target, str)
        or len(target.split("/")) != 2
        or not all(target.split("/"))
        or type(repository_id) is not int
        or repository_id <= 0
        or branch != _CANDIDATE_BRANCH
        or not isinstance(commit_sha, str)
        or _SHA.fullmatch(commit_sha) is None
        or not isinstance(snapshot_id, str)
        or _SHA.fullmatch(snapshot_id) is None
        or not isinstance(stage_id, str)
        or _SHA.fullmatch(stage_id) is None
        or snapshot_id != snapshot.snapshot_id
    ):
        raise CandidateStageError("candidate stage manifest identity is invalid")

    core = _candidate_manifest_core(
        target=target,
        repository_id=repository_id,
        commit_sha=commit_sha,
        snapshot_id=snapshot_id,
    )
    expected_stage_id = _stage_id(core)
    if stage_id != expected_stage_id:
        raise CandidateStageError("candidate stage manifest digest is invalid")
    if raw != _manifest_bytes({**core, "stage_id": stage_id}):
        raise CandidateStageError("candidate stage manifest is not canonical")

    return CandidateStage(
        stage_id=stage_id,
        root=root,
        tree_path=snapshot.tree_path,
        manifest_path=manifest_path,
        target=target,
        repository_id=repository_id,
        branch=_CANDIDATE_BRANCH,
        commit_sha=commit_sha,
        snapshot_id=snapshot_id,
    )


def _verify_fetched_candidate(fetched: CandidateFetch) -> CandidateFetch:
    if type(fetched) is not CandidateFetch:
        raise CandidateStageError("candidate fetch evidence is invalid")
    if (
        not isinstance(fetched.target, str)
        or len(fetched.target.split("/")) != 2
        or not all(fetched.target.split("/"))
        or type(fetched.repository_id) is not int
        or fetched.repository_id <= 0
        or fetched.branch != _CANDIDATE_BRANCH
        or not isinstance(fetched.commit_sha, str)
        or _SHA.fullmatch(fetched.commit_sha) is None
        or fetched.git_ref != _FETCH_REF
    ):
        raise CandidateStageError("candidate fetch evidence is invalid")
    root = _trusted_git_workspace(fetched.root)
    executable = _git_executable()
    resolved = _run_git_bytes(
        executable,
        root,
        ("rev-parse", "--verify", f"{_FETCH_REF}^{{commit}}"),
        max_bytes=256,
    ).decode("ascii", errors="strict").strip()
    object_type = _run_git_bytes(
        executable,
        root,
        ("cat-file", "-t", f"{_FETCH_REF}^{{commit}}"),
        max_bytes=64,
    ).decode("ascii", errors="strict").strip()
    if resolved != fetched.commit_sha or object_type != "commit":
        raise CandidateStageError("candidate fetch workspace no longer matches evidence")
    return fetched


def _list_candidate_tree(fetched: CandidateFetch) -> tuple[CandidateTreeFile, ...]:
    raw = _run_git_bytes(
        _git_executable(),
        fetched.root,
        ("ls-tree", "-rz", "-l", "--full-tree", fetched.commit_sha),
        max_bytes=_MAX_GIT_METADATA_BYTES,
    )
    records = raw.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    parsed = [_parse_tree_record(record) for record in records]
    parsed.sort(key=lambda item: item.path)
    previous = ""
    for item in parsed:
        if item.path == previous or (previous and item.path.startswith(previous + "/")):
            raise CandidateStageError("candidate tree contains colliding paths")
        previous = item.path
    return tuple(parsed)


def _parse_tree_record(record: bytes) -> CandidateTreeFile:
    try:
        header, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ValueError("missing path separator")
        fields = header.split()
        if len(fields) != 4:
            raise ValueError("unexpected header")
        mode_text, object_type, raw_oid, raw_size = fields
        mode_token = mode_text.decode("ascii")
        type_token = object_type.decode("ascii")
        oid = raw_oid.decode("ascii")
        size = int(raw_size.decode("ascii"), 10)
        path = raw_path.decode("utf-8", errors="strict")
    except (UnicodeError, ValueError):
        raise CandidateStageError("candidate tree metadata is invalid") from None
    if mode_token not in _BLOB_MODE or type_token != "blob":
        raise CandidateStageError("candidate tree contains unsupported Git entry")
    if _SHA.fullmatch(oid) is None or size < 0:
        raise CandidateStageError("candidate tree metadata is invalid")
    _validate_candidate_path(path)
    return CandidateTreeFile(path=path, blob_oid=oid, size=size, mode=_BLOB_MODE[mode_token])


def _validate_candidate_path(path: str) -> None:
    if not path or path.startswith("/") or path.endswith("/"):
        raise CandidateStageError("candidate tree path is unsafe")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts):
        raise CandidateStageError("candidate tree path is unsafe")
    if any("\x00" in part for part in parts):
        raise CandidateStageError("candidate tree path is unsafe")


def _materialize_blob(git_root: Path, item: CandidateTreeFile, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            result = subprocess.run(  # nosec B603
                _git_command(_git_executable(), ("cat-file", "blob", item.blob_oid)),
                cwd=git_root,
                env=_git_environment(_git_executable(), git_root),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
            handle.flush()
            os.fsync(handle.fileno())
        if result.returncode != 0:
            raise CandidateStageError("candidate blob materialization failed")
        metadata = destination.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != item.size
        ):
            raise CandidateStageError("candidate blob materialization is invalid")
        os.chmod(destination, item.mode)
    except CandidateStageError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        destination.unlink(missing_ok=True)
        raise CandidateStageError("candidate blob materialization failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _trusted_staging_root(path: Path, home: Path, fetch_root: Path) -> Path:
    if not isinstance(path, Path) or not isinstance(home, Path):
        raise CandidateStageError("candidate staging boundary is invalid")
    parent = _trusted_private_directory(path, "candidate staging root")
    try:
        home_resolved = home.resolve(strict=True)
        fetch_resolved = fetch_root.resolve(strict=True)
    except OSError as exc:
        raise CandidateStageError("candidate staging boundary is unavailable") from exc
    for other in (home_resolved, fetch_resolved):
        if parent == other or parent in other.parents or other in parent.parents:
            raise CandidateStageError("candidate staging root overlaps protected boundary")
    return parent


def _trusted_candidate_stage_root(path: Path) -> Path:
    root = _trusted_private_directory(path, "candidate stage")
    if not root.name.startswith(".snapshot-") or not root.name.endswith(".tmp"):
        raise CandidateStageError("candidate stage root identity is invalid")
    return root


def _trusted_git_workspace(path: Path) -> Path:
    root = _trusted_private_directory(path, "candidate fetch workspace")
    try:
        entries = {entry.name for entry in os.scandir(root)}
        git = (root / ".git").lstat()
    except OSError as exc:
        raise CandidateStageError("candidate fetch workspace is invalid") from exc
    if (
        entries != {".git"}
        or not stat.S_ISDIR(git.st_mode)
        or (root / ".git").is_symlink()
        or git.st_uid != os.geteuid()
    ):
        raise CandidateStageError("candidate fetch workspace is invalid")
    return root


def _trusted_private_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CandidateStageError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise CandidateStageError(f"{label} is unsafe")
    return resolved


def _private_subdirectory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise CandidateStageError("candidate snapshot directory cannot be prepared") from exc
    return _trusted_private_directory(path, "candidate snapshot directory")


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise CandidateStageError("Git executable is unavailable")
    return executable


def _git_command(executable: str, arguments: tuple[str, ...]) -> list[str]:
    return [
        executable,
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.file.allow=never",
        *arguments,
    ]


def _git_environment(executable: str, root: Path) -> dict[str, str]:
    return {
        "PATH": os.path.dirname(executable),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def _run_git_bytes(
    executable: str,
    root: Path,
    arguments: tuple[str, ...],
    *,
    max_bytes: int,
) -> bytes:
    try:
        process = subprocess.Popen(  # nosec B603
            _git_command(executable, arguments),
            cwd=root,
            env=_git_environment(executable, root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise CandidateStageError("confined candidate Git command could not start") from exc
    if process.stdout is None:
        process.kill()
        raise CandidateStageError("confined candidate Git output is unavailable")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 30
    output = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                raise CandidateStageError("confined candidate Git command timed out")
            events = selector.select(timeout=remaining)
            if not events:
                process.kill()
                raise CandidateStageError("confined candidate Git command timed out")
            chunk = os.read(process.stdout.fileno(), 64 * 1024)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > max_bytes:
                process.kill()
                raise CandidateStageError("confined candidate Git output exceeds limit")
        if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            raise CandidateStageError("confined candidate Git command failed")
        return bytes(output)
    except subprocess.TimeoutExpired:
        process.kill()
        raise CandidateStageError("confined candidate Git command timed out") from None
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def _candidate_manifest_bytes(fetched: CandidateFetch, snapshot: Snapshot) -> bytes:
    core = _candidate_manifest_core(
        target=fetched.target,
        repository_id=fetched.repository_id,
        commit_sha=fetched.commit_sha,
        snapshot_id=snapshot.snapshot_id,
    )
    return _manifest_bytes({**core, "stage_id": _stage_id(core)})


def _candidate_manifest_core(
    *,
    target: str,
    repository_id: int,
    commit_sha: str,
    snapshot_id: str,
) -> dict[str, object]:
    return {
        "version": _MANIFEST_VERSION,
        "target": target,
        "repository_id": repository_id,
        "branch": _CANDIDATE_BRANCH,
        "commit_sha": commit_sha,
        "snapshot_id": snapshot_id,
    }


def _stage_id(core: dict[str, object]) -> str:
    return hashlib.sha256(_manifest_bytes(core)).hexdigest()


def _manifest_bytes(document: dict[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _read_candidate_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or path.is_symlink():
            raise CandidateStageError("candidate stage manifest is unsafe")
        raw = path.read_bytes()
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except CandidateStageError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CandidateStageError("candidate stage manifest cannot be read") from exc
    if not isinstance(document, dict):
        raise CandidateStageError("candidate stage manifest is invalid")
    return document, raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
