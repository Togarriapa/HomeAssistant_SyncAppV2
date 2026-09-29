"""Read-only deploy-key authentication proof for one identity-bound private Repo B."""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import shlex
import signal
import stat
import subprocess  # nosec B404
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from .deploy_key import (
    MAX_PRIVATE_KEY_BYTES,
    DeployKeyError,
    inspect_repo_b_deploy_key,
)
from .github_repo import (
    RepoIdentity,
    RepositoryVerificationError,
    fetch_and_verify_private_repository,
)

GITHUB_ED25519_FINGERPRINT = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
_GITHUB_KNOWN_HOSTS = (
    b"github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
)
MAX_LS_REMOTE_BYTES = 1_048_576
MAX_LS_REMOTE_REFS = 4_096
MAX_STDERR_BYTES = 16_384
COMMAND_TIMEOUT_SECONDS = 20.0
_COMMIT_SHA = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_OBSERVATION_SHA256 = re.compile(r"[0-9a-f]{64}")
_TARGET = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9._-]{1,100}"
)
_SAFE_COMMAND_PATH = re.compile(r"/[A-Za-z0-9_./-]+")
_TRANSIENT_MARKERS = (
    b"could not resolve hostname",
    b"connection timed out",
    b"connection refused",
    b"connection reset",
    b"network is unreachable",
    b"no route to host",
    b"temporary failure in name resolution",
)
_AUTHENTICATION_MARKERS = (
    b"permission denied (publickey)",
    b"repository not found",
    b"host key verification failed",
    b"remote host identification has changed",
)


class DeployKeyAccessError(RuntimeError):
    """Deploy-key access could not be proven safely."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class DeployKeyAccessProof:
    """Content-free proof of one read-only, identity-bound SSH observation."""

    target: str
    repository_id: int
    key_fingerprint: str
    generation_id: str
    ref_count: int
    observation_sha256: str


@dataclass(frozen=True, slots=True)
class DeployKeyReference:
    """One canonical branch or tag reference observed through the protected key."""

    name: str
    commit_sha: str


@dataclass(frozen=True, slots=True)
class DeployKeyReferenceSnapshot:
    """Immutable reference metadata bound to one exact repository and key generation."""

    target: str
    repository_id: int
    key_fingerprint: str
    generation_id: str
    references: tuple[DeployKeyReference, ...]
    observation_sha256: str


@dataclass(frozen=True, slots=True)
class DeployKeyTransportSession:
    """Ephemeral descriptor-bound authority for one exact Repo B key generation."""

    target: str
    repository_id: int
    key_fingerprint: str
    generation_id: str
    git_executable: Path
    work_directory: Path
    ssh_command: str = dataclass_field(repr=False)
    environment: dict[str, str] = dataclass_field(repr=False)
    private_descriptor: int = dataclass_field(repr=False)


def test_repo_b_deploy_key_access(
    target: str,
    github_token: str,
    expected_repository_id: int,
    key_directory: Path,
    *,
    known_hosts_file: Path = Path("/app/github_known_hosts"),
    work_directory: Path,
    git_executable: Path = Path("/usr/bin/git"),
    ssh_executable: Path = Path("/usr/bin/ssh"),
) -> DeployKeyAccessProof:
    """Prove SSH read access only after the exact private Repo B identity is re-proven."""
    if type(expected_repository_id) is not int or expected_repository_id <= 0:
        raise DeployKeyAccessError("Expected repository identity is invalid")
    try:
        identity = fetch_and_verify_private_repository(
            target,
            github_token,
            expected_id=expected_repository_id,
        )
    except RepositoryVerificationError as error:
        raise DeployKeyAccessError(
            "Deploy key repository identity verification failed",
            transient=error.transient,
        ) from None
    _verify_identity(identity, target, expected_repository_id)
    try:
        enrollment = inspect_repo_b_deploy_key(key_directory)
    except DeployKeyError:
        raise DeployKeyAccessError("protected deploy key is invalid") from None
    _verify_known_hosts(known_hosts_file)
    _verify_private_directory(work_directory)
    _verify_executable(git_executable)
    _verify_executable(ssh_executable)
    _verify_safe_command_path(known_hosts_file)
    _verify_safe_command_path(ssh_executable)

    private_descriptor = _open_private_key(key_directory / "private_key")
    try:
        ssh_command = _ssh_command(
            ssh_executable,
            known_hosts_file,
            private_descriptor,
        )
        command = _ls_remote_command(identity.target, git_executable, ssh_command)
        environment = _git_environment(ssh_executable, work_directory)
        raw = _run_git_ls_remote(
            command,
            cwd=work_directory,
            environment=environment,
            pass_fds=(private_descriptor,),
        )
    finally:
        os.close(private_descriptor)
    ref_count = len(_parse_reference_evidence(raw))
    return DeployKeyAccessProof(
        target=identity.target,
        repository_id=identity.repository_id,
        key_fingerprint=enrollment.fingerprint,
        generation_id=enrollment.generation_id,
        ref_count=ref_count,
        observation_sha256=hashlib.sha256(raw).hexdigest(),
    )


@contextmanager
def open_repo_b_deploy_key_transport(
    proof: DeployKeyAccessProof,
    target: str,
    expected_repository_id: int,
    key_directory: Path,
    *,
    known_hosts_file: Path = Path("/app/github_known_hosts"),
    work_directory: Path,
    git_executable: Path = Path("/usr/bin/git"),
    ssh_executable: Path = Path("/usr/bin/ssh"),
) -> Iterator[DeployKeyTransportSession]:
    """Open one descriptor-bound Git authority and close it after the exact operation."""
    _validate_access_proof(proof)
    if (
        not _valid_target(target)
        or type(expected_repository_id) is not int
        or expected_repository_id <= 0
        or proof.target.casefold() != target.casefold()
        or proof.repository_id != expected_repository_id
    ):
        raise DeployKeyAccessError("Deploy key access proof does not match repository identity")
    try:
        enrollment = inspect_repo_b_deploy_key(key_directory)
    except DeployKeyError:
        raise DeployKeyAccessError("protected deploy key is invalid") from None
    if (
        enrollment.fingerprint != proof.key_fingerprint
        or enrollment.generation_id != proof.generation_id
    ):
        raise DeployKeyAccessError("Deploy key access proof does not match protected key")
    _verify_known_hosts(known_hosts_file)
    _verify_private_directory(work_directory)
    _verify_executable(git_executable)
    _verify_executable(ssh_executable)
    _verify_safe_command_path(known_hosts_file)
    _verify_safe_command_path(ssh_executable)

    private_descriptor = _open_private_key(key_directory / "private_key")
    try:
        yield DeployKeyTransportSession(
            target,
            expected_repository_id,
            enrollment.fingerprint,
            enrollment.generation_id,
            git_executable,
            work_directory,
            _ssh_command(ssh_executable, known_hosts_file, private_descriptor),
            _git_environment(ssh_executable, work_directory),
            private_descriptor,
        )
    finally:
        os.close(private_descriptor)


def read_repo_b_deploy_key_references(
    proof: DeployKeyAccessProof,
    target: str,
    expected_repository_id: int,
    key_directory: Path,
    *,
    known_hosts_file: Path = Path("/app/github_known_hosts"),
    work_directory: Path,
    git_executable: Path = Path("/usr/bin/git"),
    ssh_executable: Path = Path("/usr/bin/ssh"),
) -> DeployKeyReferenceSnapshot:
    """Read bounded canonical Git refs with one exact previously verified key generation."""
    with open_repo_b_deploy_key_transport(
        proof,
        target,
        expected_repository_id,
        key_directory,
        known_hosts_file=known_hosts_file,
        work_directory=work_directory,
        git_executable=git_executable,
        ssh_executable=ssh_executable,
    ) as session:
        raw = run_bounded_repo_b_git(
            _ls_remote_command(target, git_executable, session.ssh_command),
            cwd=session.work_directory,
            environment=session.environment,
            pass_fds=(session.private_descriptor,),
        )
    references = _parse_reference_evidence(raw)
    return DeployKeyReferenceSnapshot(
        target=target,
        repository_id=expected_repository_id,
        key_fingerprint=session.key_fingerprint,
        generation_id=session.generation_id,
        references=references,
        observation_sha256=hashlib.sha256(raw).hexdigest(),
    )


def run_bounded_repo_b_git(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    pass_fds: tuple[int, ...],
) -> bytes:
    """Run one already-confined Repo B Git command with bounded sanitized output."""
    return _run_git_ls_remote(
        command,
        cwd=cwd,
        environment=environment,
        pass_fds=pass_fds,
    )


def _validate_access_proof(proof: DeployKeyAccessProof) -> None:
    if type(proof) is not DeployKeyAccessProof:
        raise DeployKeyAccessError("Deploy key access proof is invalid")
    canonical_generation = False
    with suppress(ValueError, AttributeError):
        canonical_generation = str(UUID(proof.generation_id)) == proof.generation_id
    if (
        not _valid_target(proof.target)
        or type(proof.repository_id) is not int
        or proof.repository_id <= 0
        or _FINGERPRINT.fullmatch(proof.key_fingerprint) is None
        or not canonical_generation
        or type(proof.ref_count) is not int
        or proof.ref_count < 0
        or _OBSERVATION_SHA256.fullmatch(proof.observation_sha256) is None
    ):
        raise DeployKeyAccessError("Deploy key access proof is invalid")


def _valid_target(target: object) -> bool:
    if not isinstance(target, str) or _TARGET.fullmatch(target) is None:
        return False
    return target.split("/", 1)[1] not in {".", ".."}


def _ls_remote_command(
    target: str,
    git_executable: Path,
    ssh_command: str,
) -> tuple[str, ...]:
    return (
        str(git_executable),
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.file.allow=never",
        "-c",
        f"core.sshCommand={ssh_command}",
        "ls-remote",
        "--refs",
        _repository_url(target),
    )


def _git_environment(ssh_executable: Path, work_directory: Path) -> dict[str, str]:
    return {
        "PATH": str(ssh_executable.parent),
        "HOME": str(work_directory),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "GIT_SSH_VARIANT": "ssh",
        "LC_ALL": "C",
    }


def _verify_identity(identity: RepoIdentity, target: str, repository_id: int) -> None:
    if (
        type(identity) is not RepoIdentity
        or identity.target.casefold() != target.casefold()
        or identity.repository_id != repository_id
    ):
        raise DeployKeyAccessError("Deploy key repository identity verification failed")


def _verify_known_hosts(path: Path) -> None:
    try:
        metadata = path.lstat()
        if (
            not path.is_absolute()
            or Path(os.path.normpath(path)) != path
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o644
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_nlink != 1
            or metadata.st_size != len(_GITHUB_KNOWN_HOSTS)
        ):
            raise DeployKeyAccessError("GitHub host key pin is invalid")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise DeployKeyAccessError("GitHub host key pin is invalid")
            raw = os.read(descriptor, len(_GITHUB_KNOWN_HOSTS) + 1)
        finally:
            os.close(descriptor)
    except DeployKeyAccessError:
        raise
    except OSError:
        raise DeployKeyAccessError("GitHub host key pin is invalid") from None
    if raw != _GITHUB_KNOWN_HOSTS or _host_key_fingerprint(raw) != GITHUB_ED25519_FINGERPRINT:
        raise DeployKeyAccessError("GitHub host key pin is invalid")


def _host_key_fingerprint(raw: bytes) -> str:
    import base64
    import binascii

    try:
        fields = raw.decode("ascii").strip("\n").split(" ")
        if len(fields) != 3 or fields[:2] != ["github.com", "ssh-ed25519"]:
            raise ValueError
        blob = base64.b64decode(fields[2], validate=True)
    except (UnicodeError, ValueError, binascii.Error):
        raise DeployKeyAccessError("GitHub host key pin is invalid") from None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def _verify_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
        if (
            not path.is_absolute()
            or Path(os.path.normpath(path)) != path
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
        ):
            raise DeployKeyAccessError("Deploy key work directory is invalid")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        os.close(descriptor)
    except DeployKeyAccessError:
        raise
    except OSError:
        raise DeployKeyAccessError("Deploy key work directory is invalid") from None


def _verify_executable(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise DeployKeyAccessError("Deploy key access executable is invalid") from None
    if (
        not path.is_absolute()
        or Path(os.path.normpath(path)) != path
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_nlink != 1
        or not metadata.st_mode & stat.S_IXUSR
        or metadata.st_mode & 0o022
    ):
        raise DeployKeyAccessError("Deploy key access executable is invalid")
    _verify_safe_command_path(path)


def _verify_safe_command_path(path: Path) -> None:
    if _SAFE_COMMAND_PATH.fullmatch(str(path)) is None or ".." in path.parts:
        raise DeployKeyAccessError("Deploy key access path is invalid")


def _open_private_key(path: Path) -> int:
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or not 0 < before.st_size <= MAX_PRIVATE_KEY_BYTES
        ):
            raise DeployKeyAccessError("protected deploy key is invalid")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            os.close(descriptor)
            raise DeployKeyAccessError("protected deploy key is invalid")
        return descriptor
    except DeployKeyAccessError:
        raise
    except OSError:
        raise DeployKeyAccessError("protected deploy key is invalid") from None


def _ssh_command(executable: Path, known_hosts: Path, private_descriptor: int) -> str:
    return shlex.join(
        (
            str(executable),
            "-F",
            os.devnull,
            "-i",
            f"/proc/self/fd/{private_descriptor}",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            f"GlobalKnownHostsFile={os.devnull}",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=0",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "ConnectTimeout=10",
        )
    )


def _repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return f"ssh://git@github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"


def _run_git_ls_remote(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    pass_fds: tuple[int, ...],
) -> bytes:
    stdout = bytearray()
    stderr = bytearray()
    try:
        process = subprocess.Popen(  # nosec B603
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise DeployKeyAccessError("Deploy key access command failed") from None

    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    oversized = False
    timed_out = False
    if process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        raise DeployKeyAccessError("Deploy key access command failed")
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, (stdout, MAX_LS_REMOTE_BYTES))
            selector.register(process.stderr, selectors.EVENT_READ, (stderr, MAX_STDERR_BYTES))
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                events = selector.select(remaining)
                if not events:
                    timed_out = True
                    break
                for key, _ in events:
                    destination, limit = key.data
                    chunk = os.read(key.fd, min(65_536, limit + 1 - len(destination)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    destination.extend(chunk)
                    if len(destination) > limit:
                        oversized = True
                        break
                if oversized:
                    break
        if not timed_out and not oversized:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
            else:
                process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        timed_out = True
    except BaseException:
        _kill_process_group(process)
        raise
    finally:
        if timed_out or oversized:
            _kill_process_group(process)

    if timed_out:
        raise DeployKeyAccessError("Deploy key access transport timed out", transient=True)
    if oversized:
        raise DeployKeyAccessError("Deploy key access returned invalid reference evidence")
    if process.returncode != 0:
        _raise_command_failure(bytes(stderr))
    return bytes(stdout)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _raise_command_failure(stderr: bytes) -> None:
    lowered = stderr.lower()
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        raise DeployKeyAccessError("Deploy key access transport failed", transient=True)
    if any(marker in lowered for marker in _AUTHENTICATION_MARKERS):
        raise DeployKeyAccessError("Deploy key authentication failed")
    raise DeployKeyAccessError("Deploy key access command failed")


def _validate_reference_evidence(raw: bytes) -> int:
    """Compatibility validator for the original content-free access proof."""
    return len(_parse_reference_evidence(raw))


def _parse_reference_evidence(raw: bytes) -> tuple[DeployKeyReference, ...]:
    if len(raw) > MAX_LS_REMOTE_BYTES:
        raise DeployKeyAccessError("Deploy key access returned invalid reference evidence")
    if not raw:
        return ()
    if not raw.endswith(b"\n"):
        raise DeployKeyAccessError("Deploy key access returned invalid reference evidence")
    lines = raw[:-1].split(b"\n")
    if len(lines) > MAX_LS_REMOTE_REFS:
        raise DeployKeyAccessError("Deploy key access returned invalid reference evidence")
    previous: bytes | None = None
    references: list[DeployKeyReference] = []
    for line in lines:
        fields = line.split(b"\t")
        if len(fields) != 2 or _COMMIT_SHA.fullmatch(fields[0]) is None:
            raise DeployKeyAccessError("Deploy key access returned invalid reference evidence")
        reference = fields[1]
        if not _valid_reference(reference) or (previous is not None and reference <= previous):
            raise DeployKeyAccessError("Deploy key access returned invalid reference evidence")
        try:
            references.append(
                DeployKeyReference(
                    name=reference.decode("ascii"), commit_sha=fields[0].decode("ascii")
                )
            )
        except UnicodeError:
            raise DeployKeyAccessError(
                "Deploy key access returned invalid reference evidence"
            ) from None
        previous = reference
    return tuple(references)


def _valid_reference(reference: bytes) -> bool:
    if len(reference) > 1_024 or not reference.startswith((b"refs/heads/", b"refs/tags/")):
        return False
    if (
        reference.endswith((b"/", b".", b".lock"))
        or b".." in reference
        or b"//" in reference
        or b"@{" in reference
        or any(character < 0x20 or character == 0x7F for character in reference)
        or any(character in b" ~^:?*[\\" for character in reference)
    ):
        return False
    return all(component not in {b"", b".", b".."} for component in reference.split(b"/"))
