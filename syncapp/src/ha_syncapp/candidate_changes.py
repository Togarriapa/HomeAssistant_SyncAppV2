"""Produce deterministic candidate change evidence against trusted Repo B main."""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass

from . import candidate_fetch as fetch_module
from . import candidate_stage as stage_module
from .candidate_fetch import CandidateFetch, CandidateFetchError
from .candidate_stage import CandidateStage, CandidateStageError
from .github_repo import (
    BranchHead,
    RepositoryVerificationError,
    fetch_trusted_branch_head,
)

_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BASELINE_BRANCH = "main"
_BASELINE_REF = "refs/syncapp/candidate-baseline"
_ALLOWED_MODES = {"100644", "100755"}
_ALLOWED_STATUSES = {
    "added",
    "deleted",
    "modified",
    "mode_changed",
    "modified_and_mode_changed",
}


class CandidateChangeError(RuntimeError):
    """Exact candidate change evidence could not be established safely."""


@dataclass(frozen=True, slots=True)
class CandidateChange:
    """One deterministic path-level difference between trusted main and candidate."""

    path: str
    status: str
    baseline_mode: str | None
    baseline_object_id: str | None
    candidate_mode: str | None
    candidate_object_id: str | None


@dataclass(frozen=True, slots=True)
class CandidateChanges:
    """Identity-bound immutable change evidence for one candidate commit."""

    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    changes: tuple[CandidateChange, ...]


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    git_mode: str
    object_id: str


def detect_candidate_changes(
    fetched: CandidateFetch,
    stage: CandidateStage,
    token: str,
) -> CandidateChanges:
    """Compare the exact staged candidate with a stable, trusted Repo B main head."""
    _validate_inputs(fetched, stage)
    try:
        fetch_module._validate_token(token)
        stage_module.verify_candidate_stage(stage)
        stage_module._reprove_fetch(fetched)
    except (CandidateFetchError, CandidateStageError) as exc:
        raise CandidateChangeError("candidate evidence could not be reverified") from exc

    baseline = _trusted_main(stage, token)
    try:
        _fetch_exact_baseline(fetched, baseline, token)
        stage_module._reprove_fetch(fetched)
        stage_module.verify_candidate_stage(stage)

        candidate_tree = _read_tree(fetched, fetched.git_ref)
        baseline_tree = _read_tree(fetched, _BASELINE_REF)
        _bind_candidate_tree_to_stage(candidate_tree, stage)
        changes = _build_changes(baseline_tree, candidate_tree)

        stage_module.verify_candidate_stage(stage)
        stage_module._reprove_fetch(fetched)
        final_baseline = _trusted_main(stage, token)
        if final_baseline != baseline:
            raise CandidateChangeError("trusted main changed during candidate change detection")
        result = CandidateChanges(
            target=stage.target,
            repository_id=stage.repository_id,
            baseline_sha=baseline.commit_sha,
            candidate_sha=stage.commit_sha,
            changes=changes,
        )
        _validate_result(result)
        return result
    except CandidateChangeError:
        raise
    except (CandidateFetchError, CandidateStageError) as exc:
        raise CandidateChangeError("candidate change detection failed closed") from exc
    finally:
        _delete_baseline_ref(fetched)


def _validate_inputs(fetched: CandidateFetch, stage: CandidateStage) -> None:
    if type(fetched) is not CandidateFetch or type(stage) is not CandidateStage:
        raise CandidateChangeError("candidate change inputs are invalid")
    if (
        fetched.target != stage.target
        or fetched.repository_id != stage.repository_id
        or fetched.branch != "candidate"
        or stage.branch != "candidate"
        or fetched.commit_sha != stage.commit_sha
    ):
        raise CandidateChangeError("candidate fetch and stage evidence do not match")


def _trusted_main(stage: CandidateStage, token: str) -> BranchHead:
    try:
        head = fetch_trusted_branch_head(
            stage.target,
            token,
            expected_id=stage.repository_id,
            branch=_BASELINE_BRANCH,
        )
    except RepositoryVerificationError as exc:
        raise CandidateChangeError("trusted Repo B main could not be established") from exc
    if (
        type(head) is not BranchHead
        or head.target != stage.target
        or head.repository_id != stage.repository_id
        or head.branch != _BASELINE_BRANCH
        or _OBJECT_ID.fullmatch(head.commit_sha) is None
    ):
        raise CandidateChangeError("trusted Repo B main evidence is invalid")
    return head


def _fetch_exact_baseline(fetched: CandidateFetch, baseline: BranchHead, token: str) -> None:
    askpass = None
    try:
        executable = fetch_module._git_executable()
        askpass = fetch_module._create_askpass(fetched.root)
        refspec = f"+refs/heads/{_BASELINE_BRANCH}:{_BASELINE_REF}"
        fetch_module._run_git(
            executable,
            fetched.root,
            (
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--depth=1",
                fetch_module._repository_url(baseline.target),
                refspec,
            ),
            token=token,
            askpass=askpass,
        )
        fetched_sha = fetch_module._run_git(
            executable,
            fetched.root,
            ("rev-parse", "--verify", f"{_BASELINE_REF}^{{commit}}"),
        )
        object_type = fetch_module._run_git(
            executable,
            fetched.root,
            ("cat-file", "-t", f"{_BASELINE_REF}^{{commit}}"),
        )
        if fetched_sha != baseline.commit_sha or object_type != "commit":
            raise CandidateChangeError("fetched main does not match trusted baseline")
    except CandidateFetchError as exc:
        raise CandidateChangeError("trusted main fetch failed") from exc
    finally:
        if askpass is not None:
            fetch_module._delete_askpass(askpass)


def _read_tree(fetched: CandidateFetch, ref: str) -> tuple[_TreeEntry, ...]:
    executable = fetch_module._git_executable()
    try:
        raw = _run_git_bytes(executable, fetched, ("ls-tree", "-rz", "--full-tree", ref))
    except CandidateFetchError as exc:
        raise CandidateChangeError("candidate tree metadata could not be read") from exc
    return _parse_tree(raw)


def _run_git_bytes(
    executable: str,
    fetched: CandidateFetch,
    arguments: tuple[str, ...],
) -> bytes:
    """Run confined Git and return exact bytes for NUL-delimited tree metadata."""
    import subprocess  # nosec B404

    try:
        result = subprocess.run(  # nosec B603
            fetch_module._command(executable, arguments),
            cwd=fetched.root,
            env=fetch_module._git_environment(executable, fetched.root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateFetchError("confined Git command could not execute") from exc
    if result.returncode != 0:
        raise CandidateFetchError("confined Git command failed")
    return bytes(result.stdout)


def _parse_tree(raw: bytes) -> tuple[_TreeEntry, ...]:
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
            raise CandidateChangeError("Git tree metadata is malformed") from exc
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
    parts = path.split("/") if isinstance(path, str) else []
    if (
        not path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.casefold() == ".git" for part in parts)
        or mode not in _ALLOWED_MODES
        or object_type != "blob"
        or _OBJECT_ID.fullmatch(object_id) is None
        or path in seen
    ):
        raise CandidateChangeError("Git tree metadata contains an unsafe entry")
    for existing in seen:
        if path.startswith(existing + "/") or existing.startswith(path + "/"):
            raise CandidateChangeError("Git tree metadata contains conflicting paths")


def _bind_candidate_tree_to_stage(
    candidate_tree: tuple[_TreeEntry, ...],
    stage: CandidateStage,
) -> None:
    if len(candidate_tree) != len(stage.entries):
        raise CandidateChangeError("candidate Git tree does not match staged evidence")
    for git_entry, staged_entry in zip(candidate_tree, stage.entries, strict=True):
        if (
            git_entry.path != staged_entry.path
            or git_entry.git_mode != staged_entry.git_mode
            or git_entry.object_id != staged_entry.object_id
        ):
            raise CandidateChangeError("candidate Git tree does not match staged evidence")


def _build_changes(
    baseline: tuple[_TreeEntry, ...],
    candidate: tuple[_TreeEntry, ...],
) -> tuple[CandidateChange, ...]:
    baseline_by_path = {entry.path: entry for entry in baseline}
    candidate_by_path = {entry.path: entry for entry in candidate}
    paths = sorted(
        set(baseline_by_path) | set(candidate_by_path),
        key=lambda path: path.encode("utf-8"),
    )
    changes: list[CandidateChange] = []
    for path in paths:
        old = baseline_by_path.get(path)
        new = candidate_by_path.get(path)
        if old == new:
            continue
        if old is None:
            status = "added"
        elif new is None:
            status = "deleted"
        elif old.object_id != new.object_id and old.git_mode != new.git_mode:
            status = "modified_and_mode_changed"
        elif old.object_id != new.object_id:
            status = "modified"
        else:
            status = "mode_changed"
        changes.append(
            CandidateChange(
                path=path,
                status=status,
                baseline_mode=None if old is None else old.git_mode,
                baseline_object_id=None if old is None else old.object_id,
                candidate_mode=None if new is None else new.git_mode,
                candidate_object_id=None if new is None else new.object_id,
            )
        )
    return tuple(changes)


def _validate_result(result: CandidateChanges) -> None:
    previous: bytes | None = None
    for change in result.changes:
        encoded = change.path.encode("utf-8")
        if change.status not in _ALLOWED_STATUSES or (previous is not None and encoded <= previous):
            raise CandidateChangeError("candidate change evidence is invalid")
        previous = encoded


def _delete_baseline_ref(fetched: CandidateFetch) -> None:
    with suppress(CandidateFetchError):
        executable = fetch_module._git_executable()
        fetch_module._run_git(
            executable,
            fetched.root,
            ("update-ref", "-d", _BASELINE_REF),
        )
