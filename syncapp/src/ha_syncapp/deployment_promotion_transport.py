"""Bounded GitHub ref transport for an authorized deployment promotion."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
from pathlib import Path
from typing import NoReturn, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .candidate_fetch import (
    CandidateFetch,
    CandidateFetchError,
    fetch_trusted_candidate_with_deploy_key,
)
from .deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
    open_repo_b_deploy_key_transport,
    read_repo_b_deploy_key_references,
    run_bounded_repo_b_git,
)
from .deployment_promotion import (
    DeploymentPromotion,
    DeploymentPromotionError,
    PromotionRemoteState,
)
from .github_repo import (
    MAX_METADATA_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    RepositoryVerificationError,
    fetch_and_verify_private_repository,
    fetch_trusted_branch_head,
)

_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_GENERATION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REFERENCE = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_KNOWN_GOOD_TAG = re.compile(r"^syncapp-known-good-[0-9a-f-]{36}$")
_FETCH_REF = "refs/syncapp/candidate-fetch"
_FETCH_WORKSPACE_PREFIX = ".git-workspace-candidate-"
_ALLOWED_FETCH_CONFIG = {
    "core.repositoryformatversion": {"0"},
    "core.filemode": {"true", "false"},
    "core.bare": {"false"},
    "core.logallrefupdates": {"true"},
}


class DeploymentPromotionTransportError(RuntimeError):
    """GitHub promotion transport failed without exposing response details."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def read_promotion_remote_state_with_deploy_key(
    proof: DeployKeyAccessProof,
    target: str,
    repository_id: int,
    tag: str,
    key_directory: Path,
    *,
    work_directory: Path,
    known_hosts_file: Path = Path("/app/github_known_hosts"),
    git_executable: Path = Path("/usr/bin/git"),
    ssh_executable: Path = Path("/usr/bin/ssh"),
) -> PromotionRemoteState:
    """Read exact promotion refs through one previously proven key generation."""
    _validate_proof_identity(proof, target, repository_id)
    if not isinstance(tag, str) or _KNOWN_GOOD_TAG.fullmatch(tag) is None:
        raise DeploymentPromotionTransportError("promotion tag authority is invalid")
    try:
        snapshot = read_repo_b_deploy_key_references(
            proof,
            target,
            repository_id,
            key_directory,
            known_hosts_file=known_hosts_file,
            work_directory=work_directory,
            git_executable=git_executable,
            ssh_executable=ssh_executable,
        )
    except DeployKeyAccessError as error:
        raise _access_error(error) from None
    return _promotion_state_from_snapshot(snapshot, proof, target, repository_id, tag)


def publish_promotion_refs_with_deploy_key(
    intent: DeploymentPromotion,
    state: PromotionRemoteState,
    proof: DeployKeyAccessProof,
    key_directory: Path,
    workspace_root: Path,
    home_assistant_root: Path,
    *,
    known_hosts_file: Path = Path("/app/github_known_hosts"),
    git_executable: Path = Path("/usr/bin/git"),
    ssh_executable: Path = Path("/usr/bin/ssh"),
) -> None:
    """Atomically publish only missing promotion refs through an exact deploy key."""
    if type(intent) is not DeploymentPromotion or type(state) is not PromotionRemoteState:
        raise DeploymentPromotionTransportError("promotion publication authority is invalid")
    try:
        intent.validate()
        state.validate()
    except (DeploymentPromotionError, AttributeError, ValueError):
        raise DeploymentPromotionTransportError(
            "promotion publication authority is invalid"
        ) from None
    _validate_proof_identity(proof, intent.target, intent.repository_id)

    fresh = read_promotion_remote_state_with_deploy_key(
        proof,
        intent.target,
        intent.repository_id,
        intent.known_good_tag,
        key_directory,
        work_directory=workspace_root,
        known_hosts_file=known_hosts_file,
        git_executable=git_executable,
        ssh_executable=ssh_executable,
    )
    _validate_authorized_state(intent, fresh)
    if fresh != state:
        raise DeploymentPromotionTransportError(
            "promotion remote state changed after authorization"
        )
    if _promotion_complete(intent, fresh):
        return

    fetched: CandidateFetch | None = None
    cleanup_root: Path | None = None
    try:
        try:
            fetched = fetch_trusted_candidate_with_deploy_key(
                proof,
                intent.target,
                intent.repository_id,
                intent.candidate_sha,
                key_directory,
                workspace_root,
                home_assistant_root,
                known_hosts_file=known_hosts_file,
                git_executable=git_executable,
                ssh_executable=ssh_executable,
            )
        except CandidateFetchError as error:
            raise _candidate_fetch_error(error) from None
        cleanup_root = _safe_cleanup_root(fetched, workspace_root)
        _verify_fetched_candidate(
            fetched,
            intent,
            workspace_root,
            home_assistant_root,
            git_executable,
        )

        immediate = read_promotion_remote_state_with_deploy_key(
            proof,
            intent.target,
            intent.repository_id,
            intent.known_good_tag,
            key_directory,
            work_directory=fetched.root,
            known_hosts_file=known_hosts_file,
            git_executable=git_executable,
            ssh_executable=ssh_executable,
        )
        _validate_authorized_state(intent, immediate)
        if _promotion_complete(intent, immediate):
            return
        if immediate != fresh:
            raise DeploymentPromotionTransportError(
                "promotion remote state changed before publication"
            )
        _verify_fetched_candidate(
            fetched,
            intent,
            workspace_root,
            home_assistant_root,
            git_executable,
        )
        refspecs = _missing_refspecs(intent, immediate)
        try:
            with open_repo_b_deploy_key_transport(
                proof,
                intent.target,
                intent.repository_id,
                key_directory,
                known_hosts_file=known_hosts_file,
                work_directory=fetched.root,
                git_executable=git_executable,
                ssh_executable=ssh_executable,
            ) as session:
                run_bounded_repo_b_git(
                    _deploy_key_promotion_command(
                        session.git_executable,
                        session.ssh_command,
                        intent.target,
                        refspecs,
                    ),
                    cwd=fetched.root,
                    environment=session.environment,
                    pass_fds=(session.private_descriptor,),
                )
        except DeployKeyAccessError as error:
            raise _access_error(error) from None

        after = read_promotion_remote_state_with_deploy_key(
            proof,
            intent.target,
            intent.repository_id,
            intent.known_good_tag,
            key_directory,
            work_directory=fetched.root,
            known_hosts_file=known_hosts_file,
            git_executable=git_executable,
            ssh_executable=ssh_executable,
        )
        _validate_after_publication(intent, after)
    finally:
        if cleanup_root is not None:
            shutil.rmtree(cleanup_root, ignore_errors=True)


def _validate_proof_identity(proof: DeployKeyAccessProof, target: str, repository_id: int) -> None:
    if (
        type(proof) is not DeployKeyAccessProof
        or not isinstance(target, str)
        or proof.target.casefold() != target.casefold()
        or type(repository_id) is not int
        or repository_id <= 0
        or proof.repository_id != repository_id
        or _FINGERPRINT.fullmatch(proof.key_fingerprint) is None
        or _GENERATION.fullmatch(proof.generation_id) is None
        or type(proof.ref_count) is not int
        or proof.ref_count < 0
        or _SHA256.fullmatch(proof.observation_sha256) is None
    ):
        raise DeploymentPromotionTransportError("deploy-key promotion authority is invalid")


def _promotion_state_from_snapshot(
    snapshot: DeployKeyReferenceSnapshot,
    proof: DeployKeyAccessProof,
    target: str,
    repository_id: int,
    tag: str,
) -> PromotionRemoteState:
    if (
        type(snapshot) is not DeployKeyReferenceSnapshot
        or snapshot.target.casefold() != target.casefold()
        or snapshot.repository_id != repository_id
        or snapshot.key_fingerprint != proof.key_fingerprint
        or snapshot.generation_id != proof.generation_id
        or _SHA256.fullmatch(snapshot.observation_sha256) is None
        or type(snapshot.references) is not tuple
        or not _canonical_references(snapshot.references)
    ):
        raise DeploymentPromotionTransportError("promotion reference evidence is invalid")
    candidates = _references_named(snapshot.references, "refs/heads/candidate")
    mains = _references_named(snapshot.references, "refs/heads/main")
    tags = _references_named(snapshot.references, f"refs/tags/{tag}")
    if len(candidates) != 1 or len(mains) != 1 or len(tags) > 1:
        raise DeploymentPromotionTransportError("promotion reference evidence is invalid")
    result = PromotionRemoteState(
        candidates[0].commit_sha,
        mains[0].commit_sha,
        None if not tags else tags[0].commit_sha,
    )
    try:
        result.validate()
    except (DeploymentPromotionError, AttributeError, ValueError):
        raise DeploymentPromotionTransportError("promotion reference evidence is invalid") from None
    return result


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


def _references_named(
    references: tuple[DeployKeyReference, ...], name: str
) -> tuple[DeployKeyReference, ...]:
    return tuple(reference for reference in references if reference.name == name)


def _validate_authorized_state(intent: DeploymentPromotion, state: PromotionRemoteState) -> None:
    if (
        state.candidate_sha != intent.candidate_sha
        or state.main_sha not in {intent.baseline_sha, intent.candidate_sha}
        or state.tag_sha not in {None, intent.candidate_sha}
    ):
        raise DeploymentPromotionTransportError("promotion remote refs diverged")


def _promotion_complete(intent: DeploymentPromotion, state: PromotionRemoteState) -> bool:
    return state.main_sha == intent.candidate_sha and state.tag_sha == intent.candidate_sha


def _missing_refspecs(intent: DeploymentPromotion, state: PromotionRemoteState) -> tuple[str, ...]:
    result: list[str] = []
    if state.main_sha == intent.baseline_sha:
        result.append(f"{_FETCH_REF}:refs/heads/main")
    if state.tag_sha is None:
        result.append(f"{_FETCH_REF}:refs/tags/{intent.known_good_tag}")
    if not result:
        raise DeploymentPromotionTransportError("promotion publication is already complete")
    return tuple(result)


def _verify_fetched_candidate(
    fetched: CandidateFetch,
    intent: DeploymentPromotion,
    workspace_root: Path,
    home_assistant_root: Path,
    git_executable: Path,
) -> None:
    if type(fetched) is not CandidateFetch:
        raise DeploymentPromotionTransportError("isolated promotion workspace is invalid")
    try:
        root = fetched.root.resolve(strict=True)
        parent = workspace_root.resolve(strict=True)
        home = home_assistant_root.resolve(strict=True)
        metadata = root.lstat()
        git_metadata = (root / ".git").lstat()
        entries = {entry.name for entry in os.scandir(root)}
    except OSError:
        raise DeploymentPromotionTransportError("isolated promotion workspace is invalid") from None
    if (
        fetched.target.casefold() != intent.target.casefold()
        or fetched.repository_id != intent.repository_id
        or fetched.branch != "candidate"
        or fetched.commit_sha != intent.candidate_sha
        or fetched.git_ref != _FETCH_REF
        or root.parent != parent
        or root == home
        or root in home.parents
        or home in root.parents
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
        or fetched.root.is_symlink()
        or entries != {".git"}
        or not stat.S_ISDIR(git_metadata.st_mode)
        or (root / ".git").is_symlink()
        or git_metadata.st_uid != os.geteuid()
    ):
        raise DeploymentPromotionTransportError("isolated promotion workspace is invalid")
    _verify_fetch_git_config(root, git_executable)
    commit = _run_local_git(
        root, git_executable, ("rev-parse", "--verify", f"{_FETCH_REF}^{{commit}}")
    )
    object_type = _run_local_git(
        root, git_executable, ("cat-file", "-t", f"{_FETCH_REF}^{{commit}}")
    )
    if commit != intent.candidate_sha or object_type != "commit":
        raise DeploymentPromotionTransportError("isolated promotion candidate changed")


def _safe_cleanup_root(fetched: CandidateFetch, workspace_root: Path) -> Path:
    if type(fetched) is not CandidateFetch or not isinstance(fetched.root, Path):
        raise DeploymentPromotionTransportError("isolated promotion workspace is invalid")
    try:
        root = fetched.root.resolve(strict=True)
        parent = workspace_root.resolve(strict=True)
    except OSError:
        raise DeploymentPromotionTransportError("isolated promotion workspace is invalid") from None
    if root.parent != parent or not root.name.startswith(_FETCH_WORKSPACE_PREFIX) or root == parent:
        raise DeploymentPromotionTransportError("isolated promotion workspace is invalid")
    return root


def _verify_fetch_git_config(root: Path, git_executable: Path) -> None:
    raw = _run_local_git(
        root,
        git_executable,
        ("config", "--local", "--null", "--list"),
        strip=False,
    )
    try:
        if not raw.endswith("\0"):
            raise ValueError
        observed: dict[str, str] = {}
        for entry in raw[:-1].split("\0"):
            key, value = entry.split("\n", 1)
            if key in observed or value not in _ALLOWED_FETCH_CONFIG.get(key, set()):
                raise ValueError
            observed[key] = value
    except ValueError:
        raise DeploymentPromotionTransportError(
            "isolated promotion Git metadata is unsafe"
        ) from None
    if set(observed) != set(_ALLOWED_FETCH_CONFIG):
        raise DeploymentPromotionTransportError("isolated promotion Git metadata is unsafe")


def _run_local_git(
    root: Path,
    git_executable: Path,
    arguments: tuple[str, ...],
    *,
    strip: bool = True,
) -> str:
    command = (
        str(git_executable),
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.file.allow=never",
        *arguments,
    )
    environment = {
        "PATH": str(git_executable.parent),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(  # nosec B603
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        raise DeploymentPromotionTransportError(
            "isolated promotion Git verification failed"
        ) from None
    if result.returncode != 0:
        raise DeploymentPromotionTransportError("isolated promotion Git verification failed")
    return result.stdout.strip() if strip else result.stdout


def _deploy_key_promotion_command(
    executable: Path,
    ssh_command: str,
    target: str,
    refspecs: tuple[str, ...],
) -> tuple[str, ...]:
    owner, repository = target.split("/", 1)
    repository_url = (
        f"ssh://git@github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"
    )
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
        "--atomic",
        "--porcelain",
        "--no-verify",
        repository_url,
        *refspecs,
    )


def _validate_after_publication(intent: DeploymentPromotion, state: PromotionRemoteState) -> None:
    if _promotion_complete(intent, state) and state.candidate_sha == intent.candidate_sha:
        return
    if (
        state.candidate_sha == intent.candidate_sha
        and state.main_sha in {intent.baseline_sha, intent.candidate_sha}
        and state.tag_sha in {None, intent.candidate_sha}
    ):
        raise DeploymentPromotionTransportError(
            "promotion publication could not be confirmed", transient=True
        )
    raise DeploymentPromotionTransportError("promotion remote refs diverged")


def _candidate_fetch_error(error: CandidateFetchError) -> DeploymentPromotionTransportError:
    if error.transient:
        return DeploymentPromotionTransportError(
            "promotion candidate fetch is temporarily unavailable", transient=True
        )
    return DeploymentPromotionTransportError("promotion candidate fetch authority is invalid")


def _access_error(error: DeployKeyAccessError) -> DeploymentPromotionTransportError:
    if error.transient:
        return DeploymentPromotionTransportError(
            "promotion deploy-key transport is temporarily unavailable", transient=True
        )
    return DeploymentPromotionTransportError("deploy-key promotion authority is invalid")


def read_promotion_remote_state(
    target: str, token: str, repository_id: int, tag: str
) -> PromotionRemoteState:
    """Re-prove repository identity and read the exact candidate/main/tag refs."""
    try:
        identity = fetch_and_verify_private_repository(target, token, expected_id=repository_id)
        candidate = fetch_trusted_branch_head(
            identity.target, token, expected_id=repository_id, branch="candidate"
        )
        main = fetch_trusted_branch_head(
            identity.target, token, expected_id=repository_id, branch="main"
        )
        tag_sha = _fetch_tag(identity.target, token, tag)
        result = PromotionRemoteState(candidate.commit_sha, main.commit_sha, tag_sha)
        result.validate()
        return result
    except DeploymentPromotionTransportError:
        raise
    except (RepositoryVerificationError, AttributeError, ValueError):
        raise DeploymentPromotionTransportError("promotion remote state is unavailable") from None


def publish_promotion_refs(
    intent: DeploymentPromotion, state: PromotionRemoteState, token: str
) -> None:
    """Publish only missing authorized refs, never forcing an existing ref."""
    try:
        intent.validate()
        state.validate()
        identity = fetch_and_verify_private_repository(
            intent.target, token, expected_id=intent.repository_id
        )
        if state.candidate_sha != intent.candidate_sha:
            _invalid()
        if state.main_sha == intent.baseline_sha:
            _write_ref(
                identity.target,
                token,
                f"heads/{quote('main', safe='')}",
                intent.candidate_sha,
                create=False,
            )
        elif state.main_sha != intent.candidate_sha:
            _invalid()
        if state.tag_sha is None:
            _write_ref(
                identity.target,
                token,
                f"tags/{quote(intent.known_good_tag, safe='')}",
                intent.candidate_sha,
                create=True,
            )
        elif state.tag_sha != intent.candidate_sha:
            _invalid()
    except DeploymentPromotionTransportError:
        raise
    except (RepositoryVerificationError, AttributeError, ValueError):
        raise DeploymentPromotionTransportError("promotion publication is unavailable") from None


def _fetch_tag(target: str, token: str, tag: str) -> str | None:
    owner, repository = target.split("/", 1)
    url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/git/ref/tags/{quote(tag, safe='')}"
    )
    value = _request_json(url, token, "GET", None, allow_not_found=True)
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("ref") != f"refs/tags/{tag}":
        _invalid()
    target_object = value.get("object")
    if not isinstance(target_object, dict) or target_object.get("type") != "commit":
        _invalid()
    sha = target_object.get("sha")
    if not isinstance(sha, str):
        _invalid()
    return sha


def _write_ref(
    target: str,
    token: str,
    ref_suffix: str,
    sha: str,
    *,
    create: bool,
) -> None:
    owner, repository = target.split("/", 1)
    base = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/git/refs"
    )
    if create:
        url = base
        payload: object = {"ref": f"refs/{ref_suffix}", "sha": sha}
        method = "POST"
    else:
        url = f"{base}/{ref_suffix}"
        payload = {"force": False, "sha": sha}
        method = "PATCH"
    value = _request_json(url, token, method, payload)
    if not isinstance(value, dict):
        _invalid()
    expected_ref = f"refs/{ref_suffix}"
    target_object = value.get("object")
    if (
        value.get("ref") != expected_ref
        or not isinstance(target_object, dict)
        or target_object.get("sha") != sha
    ):
        _invalid()


def _request_json(
    url: str,
    token: str,
    method: str,
    payload: object | None,
    *,
    allow_not_found: bool = False,
) -> object | None:
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("ascii")
    request = Request(
        url,
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "HomeAssistant-SyncAppV2",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            raw = response.read(MAX_METADATA_BYTES + 1)
    except HTTPError as error:
        if allow_not_found and error.code == 404:
            return None
        raise DeploymentPromotionTransportError(
            f"promotion GitHub request failed with HTTP {error.code}"
        ) from None
    except (URLError, TimeoutError, OSError):
        raise DeploymentPromotionTransportError(
            "promotion GitHub transport is unavailable"
        ) from None
    if len(raw) > MAX_METADATA_BYTES:
        _invalid()
    try:
        return cast(object, json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object))
    except (UnicodeError, ValueError, RecursionError):
        _invalid()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _invalid() -> NoReturn:
    raise DeploymentPromotionTransportError("promotion GitHub state is invalid") from None
