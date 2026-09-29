"""Non-force Repo B publication transport from an isolated Git workspace."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess  # nosec B404
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote

from ha_syncapp.deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
    open_repo_b_deploy_key_transport,
    read_repo_b_deploy_key_references,
    run_bounded_repo_b_git,
)
from ha_syncapp.git_workspace import GitWorkspace, WorkspaceError, verify_workspace_content
from ha_syncapp.github_repo import BranchAbsence, BranchHead
from ha_syncapp.local_git import GitError, inspect_repository
from ha_syncapp.publication_intent import PublicationIntent

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_GENERATION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_REFERENCE = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_TOKEN = re.compile(r"^[!-~]{1,512}$")
_ALLOWED_LOCAL_CONFIG = {
    "core.repositoryformatversion": {"0"},
    "core.filemode": {"true", "false"},
    "core.bare": {"false"},
    "core.logallrefupdates": {"true"},
    "user.name": {"Home Assistant SyncApp"},
    "user.email": {"syncapp@localhost"},
}


class PublicationTransportError(RuntimeError):
    """An authorized Repo B publication could not be safely transported."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def push_publication_intent(
    workspace: GitWorkspace,
    intent: PublicationIntent,
    current_remote: BranchHead | BranchAbsence,
    token: str,
) -> str:
    """Push exactly one authorized commit without force after fresh remote proof."""
    _validate_intent(intent)
    _validate_current_remote(intent, current_remote)
    _validate_token(token)
    try:
        repository = inspect_repository(workspace)
        snapshot_id = verify_workspace_content(workspace)
    except (GitError, WorkspaceError) as exc:
        raise PublicationTransportError(
            "isolated publication workspace could not be re-proven"
        ) from exc
    if repository.default_branch != intent.branch:
        raise PublicationTransportError("publication branch does not match isolated workspace")
    if snapshot_id != workspace.snapshot_id:
        raise PublicationTransportError("isolated publication snapshot identity changed")

    root = workspace.root.resolve(strict=True)
    tree = workspace.tree_path.resolve(strict=True)
    executable = _git_executable()
    try:
        local_head = _run_git(executable, tree, root, ("rev-parse", "--verify", "HEAD"))
    except GitError as exc:
        raise PublicationTransportError("local publication commit could not be re-proven") from exc
    if local_head != intent.local_commit_sha:
        raise PublicationTransportError("local publication commit changed after authorization")

    askpass = _create_askpass(root)
    try:
        repository_url = _repository_url(intent.target)
        refspec = f"{intent.local_commit_sha}:refs/heads/{intent.branch}"
        _run_git(
            executable,
            tree,
            root,
            (
                "push",
                "--porcelain",
                "--no-verify",
                repository_url,
                refspec,
            ),
            token=token,
            askpass=askpass,
        )
    except GitError as exc:
        _verify_after_transport(workspace)
        raise PublicationTransportError("Repo B publication transport was rejected") from exc
    finally:
        with suppress(OSError):
            askpass.unlink(missing_ok=True)

    _verify_after_transport(workspace)
    return intent.local_commit_sha


def push_publication_intent_with_deploy_key(
    workspace: GitWorkspace,
    intent: PublicationIntent,
    proof: DeployKeyAccessProof,
    key_directory: Path,
    *,
    known_hosts_file: Path = Path("/app/github_known_hosts"),
    git_executable: Path = Path("/usr/bin/git"),
    ssh_executable: Path = Path("/usr/bin/ssh"),
    require_repository_empty: bool = False,
) -> str:
    """Publish one immutable intent through an exact protected key generation."""
    _validate_intent(intent)
    _validate_deploy_key_proof(intent, proof)
    before = _read_deploy_key_references(
        workspace,
        intent,
        proof,
        key_directory,
        known_hosts_file=known_hosts_file,
        git_executable=git_executable,
        ssh_executable=ssh_executable,
    )
    if type(require_repository_empty) is not bool:
        raise PublicationTransportError("publication repository policy is invalid")
    _validate_before_publication(
        before,
        intent,
        proof,
        require_repository_empty=require_repository_empty,
    )
    root, tree, local_head = _reprove_workspace(workspace, intent, git_executable)
    if local_head != intent.local_commit_sha:
        raise PublicationTransportError("local publication commit changed after authorization")

    try:
        with open_repo_b_deploy_key_transport(
            proof,
            intent.target,
            intent.repository_id,
            key_directory,
            known_hosts_file=known_hosts_file,
            work_directory=tree,
            git_executable=git_executable,
            ssh_executable=ssh_executable,
        ) as session:
            run_bounded_repo_b_git(
                _deploy_key_push_command(
                    session.git_executable,
                    session.ssh_command,
                    intent.target,
                    intent.local_commit_sha,
                    intent.branch,
                ),
                cwd=tree,
                environment=session.environment,
                pass_fds=(session.private_descriptor,),
            )
    except DeployKeyAccessError as error:
        raise _deploy_key_access_error(error) from None

    _verify_after_transport(workspace)
    after = _read_deploy_key_references(
        workspace,
        intent,
        proof,
        key_directory,
        known_hosts_file=known_hosts_file,
        git_executable=git_executable,
        ssh_executable=ssh_executable,
    )
    _validate_after_publication(after, intent, proof)
    return intent.local_commit_sha


def _validate_deploy_key_proof(intent: PublicationIntent, proof: DeployKeyAccessProof) -> None:
    if (
        type(proof) is not DeployKeyAccessProof
        or proof.target.casefold() != intent.target.casefold()
        or proof.repository_id != intent.repository_id
        or _FINGERPRINT.fullmatch(proof.key_fingerprint) is None
        or _GENERATION.fullmatch(proof.generation_id) is None
        or type(proof.ref_count) is not int
        or proof.ref_count < 0
        or _SHA256.fullmatch(proof.observation_sha256) is None
    ):
        raise PublicationTransportError("deploy-key publication authority is invalid")


def _read_deploy_key_references(
    workspace: GitWorkspace,
    intent: PublicationIntent,
    proof: DeployKeyAccessProof,
    key_directory: Path,
    *,
    known_hosts_file: Path,
    git_executable: Path,
    ssh_executable: Path,
) -> DeployKeyReferenceSnapshot:
    try:
        return read_repo_b_deploy_key_references(
            proof,
            intent.target,
            intent.repository_id,
            key_directory,
            known_hosts_file=known_hosts_file,
            work_directory=workspace.tree_path,
            git_executable=git_executable,
            ssh_executable=ssh_executable,
        )
    except DeployKeyAccessError as error:
        raise _deploy_key_access_error(error) from None


def _validate_reference_snapshot(
    snapshot: DeployKeyReferenceSnapshot,
    intent: PublicationIntent,
    proof: DeployKeyAccessProof,
) -> tuple[DeployKeyReference, ...]:
    if (
        type(snapshot) is not DeployKeyReferenceSnapshot
        or snapshot.target.casefold() != intent.target.casefold()
        or snapshot.repository_id != intent.repository_id
        or snapshot.key_fingerprint != proof.key_fingerprint
        or snapshot.generation_id != proof.generation_id
        or _SHA256.fullmatch(snapshot.observation_sha256) is None
        or type(snapshot.references) is not tuple
        or not _canonical_references(snapshot.references)
    ):
        raise PublicationTransportError("publication reference evidence is invalid")
    name = f"refs/heads/{intent.branch}"
    return tuple(reference for reference in snapshot.references if reference.name == name)


def _canonical_references(references: tuple[DeployKeyReference, ...]) -> bool:
    previous: str | None = None
    for reference in references:
        if (
            type(reference) is not DeployKeyReference
            or not _valid_reference_name(reference.name)
            or _COMMIT_SHA.fullmatch(reference.commit_sha) is None
            or (previous is not None and reference.name <= previous)
        ):
            return False
        previous = reference.name
    return True


def _valid_reference_name(name: str) -> bool:
    if (
        not isinstance(name, str)
        or _REFERENCE.fullmatch(name) is None
        or name.endswith(("/", ".", ".lock"))
        or ".." in name
        or "//" in name
        or "@{" in name
        or any(character in " ~^:?*[\\" or ord(character) < 0x20 for character in name)
    ):
        return False
    return all(component not in {"", ".", ".."} for component in name.split("/"))


def _validate_before_publication(
    snapshot: DeployKeyReferenceSnapshot,
    intent: PublicationIntent,
    proof: DeployKeyAccessProof,
    *,
    require_repository_empty: bool = False,
) -> None:
    if require_repository_empty and snapshot.references:
        raise PublicationTransportError("publication repository is no longer empty")
    references = _validate_reference_snapshot(snapshot, intent, proof)
    if intent.expect_remote_absent:
        if references:
            raise PublicationTransportError("publication target branch is no longer absent")
        return
    if len(references) != 1 or references[0].commit_sha != intent.expected_remote_commit_sha:
        raise PublicationTransportError("publication branch evidence changed after authorization")


def _validate_after_publication(
    snapshot: DeployKeyReferenceSnapshot,
    intent: PublicationIntent,
    proof: DeployKeyAccessProof,
) -> None:
    references = _validate_reference_snapshot(snapshot, intent, proof)
    if len(references) == 1 and references[0].commit_sha == intent.local_commit_sha:
        return
    if not references and intent.expect_remote_absent:
        raise PublicationTransportError("Repo B publication could not be confirmed", transient=True)
    if (
        len(references) == 1
        and not intent.expect_remote_absent
        and references[0].commit_sha == intent.expected_remote_commit_sha
    ):
        raise PublicationTransportError("Repo B publication could not be confirmed", transient=True)
    raise PublicationTransportError("Repo B publication target diverged")


def _reprove_workspace(
    workspace: GitWorkspace, intent: PublicationIntent, git_executable: Path
) -> tuple[Path, Path, str]:
    if type(workspace) is not GitWorkspace:
        raise PublicationTransportError("isolated publication workspace is invalid")
    try:
        repository = inspect_repository(workspace)
        snapshot_id = verify_workspace_content(workspace)
    except (GitError, WorkspaceError) as exc:
        raise PublicationTransportError(
            "isolated publication workspace could not be re-proven"
        ) from exc
    if repository.default_branch != intent.branch:
        raise PublicationTransportError("publication branch does not match isolated workspace")
    if snapshot_id != workspace.snapshot_id:
        raise PublicationTransportError("isolated publication snapshot identity changed")
    root = workspace.root.resolve(strict=True)
    tree = workspace.tree_path.resolve(strict=True)
    _verify_local_git_config(str(git_executable), tree, root)
    try:
        local_head = _run_git(str(git_executable), tree, root, ("rev-parse", "--verify", "HEAD"))
    except GitError as exc:
        raise PublicationTransportError("local publication commit could not be re-proven") from exc
    return root, tree, local_head


def _verify_local_git_config(executable: str, tree: Path, root: Path) -> None:
    try:
        raw = _run_git(executable, tree, root, ("config", "--local", "--null", "--list"))
        if not raw.endswith("\0"):
            raise ValueError
        entries = raw[:-1].split("\0")
        observed: dict[str, str] = {}
        for entry in entries:
            key, value = entry.split("\n", 1)
            if key in observed or value not in _ALLOWED_LOCAL_CONFIG.get(key, set()):
                raise ValueError
            observed[key] = value
    except (GitError, ValueError):
        raise PublicationTransportError("isolated publication Git metadata is unsafe") from None
    if set(observed) != set(_ALLOWED_LOCAL_CONFIG):
        raise PublicationTransportError("isolated publication Git metadata is unsafe")


def _deploy_key_push_command(
    executable: Path,
    ssh_command: str,
    target: str,
    commit_sha: str,
    branch: str,
) -> tuple[str, ...]:
    return (
        str(executable),
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "push.recurseSubmodules=no",
        "-c",
        f"core.sshCommand={ssh_command}",
        "push",
        "--porcelain",
        "--no-verify",
        _ssh_repository_url(target),
        f"{commit_sha}:refs/heads/{branch}",
    )


def _deploy_key_access_error(error: DeployKeyAccessError) -> PublicationTransportError:
    if error.transient:
        return PublicationTransportError(
            "Repo B publication transport is temporarily unavailable", transient=True
        )
    return PublicationTransportError("deploy-key publication authority is invalid")


def _validate_intent(intent: PublicationIntent) -> None:
    if type(intent) is not PublicationIntent:
        raise PublicationTransportError("publication intent evidence is invalid")
    if type(intent.repository_id) is not int or intent.repository_id <= 0:
        raise PublicationTransportError("publication repository identity is invalid")
    if not isinstance(intent.target, str) or _TARGET.fullmatch(intent.target) is None:
        raise PublicationTransportError("publication repository target is invalid")
    if not isinstance(intent.branch, str) or _BRANCH.fullmatch(intent.branch) is None:
        raise PublicationTransportError("publication branch identity is invalid")
    if _COMMIT_SHA.fullmatch(intent.local_commit_sha) is None:
        raise PublicationTransportError("publication local commit identity is invalid")
    if intent.expect_remote_absent:
        if intent.expected_remote_commit_sha is not None:
            raise PublicationTransportError("publication initialization intent is inconsistent")
    elif (
        intent.expected_remote_commit_sha is None
        or _COMMIT_SHA.fullmatch(intent.expected_remote_commit_sha) is None
    ):
        raise PublicationTransportError("publication remote expectation is invalid")


def _validate_current_remote(
    intent: PublicationIntent,
    current_remote: BranchHead | BranchAbsence,
) -> None:
    if intent.expect_remote_absent:
        if type(current_remote) is not BranchAbsence:
            raise PublicationTransportError("publication target branch is no longer absent")
        _validate_remote_identity(intent, current_remote)
        return
    if type(current_remote) is not BranchHead:
        raise PublicationTransportError(
            "publication target branch is no longer at expected baseline"
        )
    _validate_remote_identity(intent, current_remote)
    if current_remote.commit_sha != intent.expected_remote_commit_sha:
        raise PublicationTransportError("publication target branch changed after authorization")


def _validate_remote_identity(
    intent: PublicationIntent,
    current_remote: BranchHead | BranchAbsence,
) -> None:
    if type(current_remote.repository_id) is not int or current_remote.repository_id <= 0:
        raise PublicationTransportError("trusted publication repository identity is invalid")
    if intent.target.casefold() != current_remote.target.casefold():
        raise PublicationTransportError("trusted publication repository target changed")
    if intent.repository_id != current_remote.repository_id:
        raise PublicationTransportError("trusted publication repository identity changed")
    if intent.branch != current_remote.branch:
        raise PublicationTransportError("trusted publication branch changed")


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise PublicationTransportError("GitHub authentication is invalid")


def _repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return f"https://github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"


def _ssh_repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return f"ssh://git@github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None or not os.path.isabs(executable):
        raise PublicationTransportError("Git executable is unavailable")
    return executable


def _create_askpass(root: Path) -> Path:
    path = root / ".syncapp-push-askpass"
    script = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *) printf '%s\\n' "$SYNCAPP_GITHUB_TOKEN" ;;
esac
"""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(script)
        metadata = path.lstat()
    except OSError as exc:
        raise PublicationTransportError("Git authentication helper could not be prepared") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise PublicationTransportError("Git authentication helper is unsafe")
    return path


def _git_environment(
    executable: str,
    root: Path,
    *,
    token: str | None = None,
    askpass: Path | None = None,
) -> dict[str, str]:
    environment = {
        "PATH": os.path.dirname(executable),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "LC_ALL": "C",
    }
    if token is not None and askpass is not None:
        environment["GIT_ASKPASS"] = str(askpass)
        environment["SYNCAPP_GITHUB_TOKEN"] = token
    return environment


def _command(executable: str, arguments: tuple[str, ...]) -> list[str]:
    return [
        executable,
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        *arguments,
    ]


def _run_git(
    executable: str,
    tree: Path,
    root: Path,
    arguments: tuple[str, ...],
    *,
    token: str | None = None,
    askpass: Path | None = None,
) -> str:
    try:
        result = subprocess.run(  # nosec B603
            _command(executable, arguments),
            cwd=tree,
            env=_git_environment(executable, root, token=token, askpass=askpass),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError("confined Git command could not execute") from exc
    if result.returncode != 0:
        raise GitError("confined Git command failed")
    return result.stdout.strip()


def _verify_after_transport(workspace: GitWorkspace) -> None:
    try:
        snapshot_id = verify_workspace_content(workspace)
    except WorkspaceError as exc:
        raise PublicationTransportError(
            "isolated publication workspace changed during transport"
        ) from exc
    if snapshot_id != workspace.snapshot_id:
        raise PublicationTransportError(
            "isolated publication snapshot identity changed during transport"
        )
