"""Proof-bound deploy-key authority for generated history retention."""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .database_history_evidence import (
    DatabaseHistoryEvidenceError,
    DatabaseHistoryRecord,
    TrustedDatabaseHistoryEvidence,
    validate_trusted_database_history_evidence,
)
from .database_history_prewrite import (
    DatabaseHistoryPrewriteError,
    TrustedDatabaseHistoryPrewrite,
    verify_database_history_prewrite,
)
from .database_history_replace_transport import (
    DatabaseHistoryReplacementArtifact,
    DatabaseHistoryReplacementTransportError,
    replace_database_history_with_deploy_key,
)
from .database_history_replacement import DatabaseHistoryReplacementAuthorization
from .database_retention import DATABASE_BRANCH, MAX_DATABASE_SNAPSHOTS
from .deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReferenceSnapshot,
    open_repo_b_deploy_key_transport,
    read_repo_b_deploy_key_references,
    run_bounded_repo_b_git,
)
from .github_repo import BranchHead
from .log_history_evidence import (
    LogHistoryEvidenceError,
    LogHistoryRecord,
    TrustedLogHistoryEvidence,
    validate_trusted_log_history_evidence,
)
from .log_history_prewrite import (
    LogHistoryPrewriteError,
    TrustedLogHistoryPrewrite,
    verify_log_history_prewrite,
)
from .log_history_replace_transport import (
    LogHistoryReplacementArtifact,
    LogHistoryReplacementTransportError,
    replace_logs_history_with_deploy_key,
)
from .log_history_replacement import LogHistoryReplacementAuthorization
from .log_history_retention import LOG_HISTORY_BRANCH, MAX_HISTORY_COMMITS

_ALLOWED_BRANCHES = frozenset((DATABASE_BRANCH, LOG_HISTORY_BRANCH))
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REFERENCE = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FETCH_REF = "refs/syncapp/retention"


class DeployKeyRetentionAuthorityError(RuntimeError):
    """Retention authority failed closed without exposing credential detail."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class _HistoryRecord:
    sha: str
    committed_at: datetime
    parent_shas: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeployKeyRetentionAuthority:
    """One immutable proof/key generation for logs and Recorder retention."""

    proof: DeployKeyAccessProof
    key_directory: Path = field(repr=False)
    access_work_directory: Path = field(repr=False)
    staging_root: Path = field(repr=False)
    known_hosts_file: Path = field(default=Path("/app/github_known_hosts"), repr=False)
    git_executable: Path = field(default=Path("/usr/bin/git"), repr=False)
    ssh_executable: Path = field(default=Path("/usr/bin/ssh"), repr=False)

    def __post_init__(self) -> None:
        paths = (
            self.key_directory,
            self.access_work_directory,
            self.staging_root,
            self.known_hosts_file,
            self.git_executable,
            self.ssh_executable,
        )
        if type(self.proof) is not DeployKeyAccessProof or any(
            not isinstance(path, Path) or not path.is_absolute() for path in paths
        ):
            raise DeployKeyRetentionAuthorityError("deploy-key retention authority is invalid")

    def observe(self, target: str, repository_id: int, branch: str) -> BranchHead:
        """Read one exact generated branch from canonical proof-bound references."""

        if branch not in _ALLOWED_BRANCHES:
            raise DeployKeyRetentionAuthorityError("retention branch authority is invalid")
        snapshot = self._references(target, repository_id)
        name = f"refs/heads/{branch}"
        matches = tuple(reference for reference in snapshot.references if reference.name == name)
        if len(matches) != 1 or _COMMIT_SHA.fullmatch(matches[0].commit_sha) is None:
            raise DeployKeyRetentionAuthorityError("retention reference evidence is invalid")
        return BranchHead(target, repository_id, branch, matches[0].commit_sha)

    def read_log_history(self, reference_time: datetime) -> TrustedLogHistoryEvidence:
        repository: Path | None = None
        try:
            head, records, repository = self._acquire(LOG_HISTORY_BRANCH)
            return validate_trusted_log_history_evidence(
                branch_head=head,
                records=tuple(
                    LogHistoryRecord(record.sha, record.committed_at, record.parent_shas)
                    for record in records
                ),
                reference_time=reference_time,
            )
        except (LogHistoryEvidenceError, ValueError):
            raise DeployKeyRetentionAuthorityError("logs retention history is invalid") from None
        finally:
            if repository is not None:
                shutil.rmtree(repository, ignore_errors=True)

    def read_database_history(
        self,
        reference_time: datetime,
        retention_days: int,
    ) -> TrustedDatabaseHistoryEvidence:
        repository: Path | None = None
        try:
            head, records, repository = self._acquire(DATABASE_BRANCH)
            return validate_trusted_database_history_evidence(
                branch_head=head,
                records=tuple(
                    DatabaseHistoryRecord(record.sha, record.committed_at, record.parent_shas)
                    for record in records
                ),
                reference_time=reference_time,
                retention_days=retention_days,
            )
        except (DatabaseHistoryEvidenceError, ValueError):
            raise DeployKeyRetentionAuthorityError(
                "database retention history is invalid"
            ) from None
        finally:
            if repository is not None:
                shutil.rmtree(repository, ignore_errors=True)

    def stage_log_history(self, evidence: TrustedLogHistoryEvidence) -> Path:
        head, records, repository = self._acquire(LOG_HISTORY_BRANCH)
        try:
            expected = tuple((commit.sha, commit.committed_at) for commit in evidence.commits)
            observed = tuple((record.sha, record.committed_at) for record in records)
            if (
                head.target.casefold() != evidence.target.casefold()
                or head.repository_id != evidence.repository_id
                or head.commit_sha != evidence.expected_head_sha
                or observed != expected
            ):
                raise DeployKeyRetentionAuthorityError("logs retention history changed")
            return repository
        except BaseException:
            shutil.rmtree(repository, ignore_errors=True)
            raise

    def stage_database_history(self, evidence: TrustedDatabaseHistoryEvidence) -> Path:
        head, records, repository = self._acquire(DATABASE_BRANCH)
        try:
            expected = tuple(
                (record.sha, record.committed_at, record.parent_shas) for record in evidence.records
            )
            observed = tuple(
                (record.sha, record.committed_at, record.parent_shas) for record in records
            )
            if (
                head.target.casefold() != evidence.target.casefold()
                or head.repository_id != evidence.repository_id
                or head.commit_sha != evidence.expected_head_sha
                or observed != expected
            ):
                raise DeployKeyRetentionAuthorityError("database retention history changed")
            return repository
        except BaseException:
            shutil.rmtree(repository, ignore_errors=True)
            raise

    def prewrite_log(self, evidence: TrustedLogHistoryEvidence) -> TrustedLogHistoryPrewrite:
        try:
            return verify_log_history_prewrite(
                evidence=evidence,
                current=self.observe(evidence.target, evidence.repository_id, LOG_HISTORY_BRANCH),
            )
        except LogHistoryPrewriteError as exc:
            raise DeployKeyRetentionAuthorityError("logs retention prewrite failed") from exc

    def prewrite_database(
        self, evidence: TrustedDatabaseHistoryEvidence
    ) -> TrustedDatabaseHistoryPrewrite:
        try:
            return verify_database_history_prewrite(
                evidence=evidence,
                current=self.observe(evidence.target, evidence.repository_id, DATABASE_BRANCH),
            )
        except DatabaseHistoryPrewriteError as exc:
            raise DeployKeyRetentionAuthorityError("database retention prewrite failed") from exc

    def replace_log(
        self,
        authorization: LogHistoryReplacementAuthorization,
        artifact: LogHistoryReplacementArtifact,
    ) -> bool:
        self._require_expected_head(
            authorization.target,
            authorization.repository_id,
            LOG_HISTORY_BRANCH,
            authorization.expected_head_sha,
        )
        try:
            replaced = replace_logs_history_with_deploy_key(
                authorization=authorization,
                artifact=artifact,
                proof=self.proof,
                key_directory=self.key_directory,
                known_hosts_file=self.known_hosts_file,
                git_executable=self.git_executable,
                ssh_executable=self.ssh_executable,
            )
        except LogHistoryReplacementTransportError as exc:
            raise DeployKeyRetentionAuthorityError(
                "logs retention publication failed", transient=exc.retryable
            ) from exc
        self._require_expected_head(
            authorization.target,
            authorization.repository_id,
            LOG_HISTORY_BRANCH,
            artifact.replacement_head_sha,
        )
        return replaced

    def replace_database(
        self,
        authorization: DatabaseHistoryReplacementAuthorization,
        artifact: DatabaseHistoryReplacementArtifact,
    ) -> bool:
        self._require_expected_head(
            authorization.target,
            authorization.repository_id,
            DATABASE_BRANCH,
            authorization.expected_head_sha,
        )
        try:
            replaced = replace_database_history_with_deploy_key(
                authorization=authorization,
                artifact=artifact,
                proof=self.proof,
                key_directory=self.key_directory,
                known_hosts_file=self.known_hosts_file,
                git_executable=self.git_executable,
                ssh_executable=self.ssh_executable,
            )
        except DatabaseHistoryReplacementTransportError as exc:
            raise DeployKeyRetentionAuthorityError(
                "database retention publication failed", transient=exc.retryable
            ) from exc
        self._require_expected_head(
            authorization.target,
            authorization.repository_id,
            DATABASE_BRANCH,
            artifact.replacement_head_sha,
        )
        return replaced

    def _require_expected_head(
        self,
        target: str,
        repository_id: int,
        branch: str,
        expected_sha: str,
    ) -> None:
        current = self.observe(target, repository_id, branch)
        if current.commit_sha != expected_sha:
            raise DeployKeyRetentionAuthorityError("retention history changed before publication")

    def _acquire(
        self,
        branch: str,
    ) -> tuple[BranchHead, tuple[_HistoryRecord, ...], Path]:
        head = self.observe(self.proof.target, self.proof.repository_id, branch)
        repository = self._create_repository(branch)
        accepted = False
        try:
            self._run_local(
                repository,
                ("init", "--quiet", "--initial-branch", branch),
            )
            try:
                with open_repo_b_deploy_key_transport(
                    self.proof,
                    head.target,
                    head.repository_id,
                    self.key_directory,
                    known_hosts_file=self.known_hosts_file,
                    work_directory=repository,
                    git_executable=self.git_executable,
                    ssh_executable=self.ssh_executable,
                ) as session:
                    run_bounded_repo_b_git(
                        (
                            str(session.git_executable),
                            "-c",
                            f"core.hooksPath={os.devnull}",
                            "-c",
                            "credential.helper=",
                            "-c",
                            "protocol.file.allow=never",
                            "-c",
                            f"core.sshCommand={session.ssh_command}",
                            "fetch",
                            "--quiet",
                            "--no-tags",
                            "--no-recurse-submodules",
                            _repository_url(head.target),
                            f"+refs/heads/{branch}:{_FETCH_REF}",
                        ),
                        cwd=repository,
                        environment=session.environment,
                        pass_fds=(session.private_descriptor,),
                    )
            except DeployKeyAccessError as exc:
                raise DeployKeyRetentionAuthorityError(
                    "retention history transport failed"
                    if exc.transient
                    else "retention history authority is invalid",
                    transient=exc.transient,
                ) from exc
            fetched = (
                self._run_local(
                    repository,
                    ("rev-parse", "--verify", f"{_FETCH_REF}^{{commit}}"),
                )
                .decode("ascii")
                .strip()
            )
            if fetched != head.commit_sha:
                raise DeployKeyRetentionAuthorityError("retention history changed during fetch")
            limit = (
                MAX_HISTORY_COMMITS + 1
                if branch == LOG_HISTORY_BRANCH
                else MAX_DATABASE_SNAPSHOTS + 1
            )
            raw = self._run_local(
                repository,
                (
                    "rev-list",
                    "--timestamp",
                    "--parents",
                    f"--max-count={limit}",
                    _FETCH_REF,
                ),
            )
            records = _parse_history(raw, head.commit_sha, limit - 1)
            accepted = True
            return head, records, repository.resolve(strict=True)
        except DeployKeyRetentionAuthorityError:
            raise
        except (OSError, UnicodeError, ValueError):
            raise DeployKeyRetentionAuthorityError("retention history acquisition failed") from None
        finally:
            if not accepted:
                shutil.rmtree(repository, ignore_errors=True)

    def _run_local(self, repository: Path, arguments: tuple[str, ...]) -> bytes:
        try:
            return run_bounded_repo_b_git(
                (
                    str(self.git_executable),
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "protocol.file.allow=never",
                    *arguments,
                ),
                cwd=repository,
                environment={
                    "PATH": str(self.git_executable.parent),
                    "HOME": str(repository),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "Never",
                    "LC_ALL": "C",
                },
                pass_fds=(),
            )
        except DeployKeyAccessError as exc:
            raise DeployKeyRetentionAuthorityError(
                "retention history command failed", transient=exc.transient
            ) from exc

    def _create_repository(self, branch: str) -> Path:
        root = _private_root(self.staging_root)
        repository = root / f".deploy-key-{branch}-retention-{uuid.uuid4().hex}.tmp"
        try:
            repository.mkdir(mode=0o700)
            os.chmod(repository, 0o700)
            return repository
        except OSError:
            raise DeployKeyRetentionAuthorityError(
                "retention staging workspace is unavailable", transient=True
            ) from None

    def _references(self, target: str, repository_id: int) -> DeployKeyReferenceSnapshot:
        try:
            snapshot = read_repo_b_deploy_key_references(
                self.proof,
                target,
                repository_id,
                self.key_directory,
                known_hosts_file=self.known_hosts_file,
                work_directory=self.access_work_directory,
                git_executable=self.git_executable,
                ssh_executable=self.ssh_executable,
            )
        except DeployKeyAccessError as exc:
            raise DeployKeyRetentionAuthorityError(
                "retention reference transport failed"
                if exc.transient
                else "retention reference authority is invalid",
                transient=exc.transient,
            ) from exc
        if (
            type(snapshot) is not DeployKeyReferenceSnapshot
            or snapshot.target.casefold() != target.casefold()
            or snapshot.repository_id != repository_id
            or snapshot.key_fingerprint != self.proof.key_fingerprint
            or snapshot.generation_id != self.proof.generation_id
            or _SHA256.fullmatch(snapshot.observation_sha256) is None
            or type(snapshot.references) is not tuple
        ):
            raise DeployKeyRetentionAuthorityError("retention reference evidence is invalid")
        previous: str | None = None
        for reference in snapshot.references:
            if (
                not isinstance(reference.name, str)
                or _REFERENCE.fullmatch(reference.name) is None
                or reference.name.endswith(("/", ".", ".lock"))
                or ".." in reference.name
                or "//" in reference.name
                or "@{" in reference.name
                or not isinstance(reference.commit_sha, str)
                or _COMMIT_SHA.fullmatch(reference.commit_sha) is None
                or (previous is not None and reference.name <= previous)
            ):
                raise DeployKeyRetentionAuthorityError("retention reference evidence is invalid")
            previous = reference.name
        return snapshot


def _private_root(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise DeployKeyRetentionAuthorityError("retention staging root is invalid")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
        ):
            raise DeployKeyRetentionAuthorityError("retention staging root is unsafe")
        os.chmod(path, 0o700)
        return path.resolve(strict=True)
    except DeployKeyRetentionAuthorityError:
        raise
    except OSError:
        raise DeployKeyRetentionAuthorityError(
            "retention staging root is unavailable", transient=True
        ) from None


def _parse_history(
    raw: bytes,
    expected_head: str,
    maximum_records: int,
) -> tuple[_HistoryRecord, ...]:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError:
        raise DeployKeyRetentionAuthorityError("retention history metadata is invalid") from None
    if not lines or len(lines) > maximum_records:
        raise DeployKeyRetentionAuthorityError("retention history exceeds the evidence limit")
    records: list[_HistoryRecord] = []
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 2 or not fields[0].isdigit():
            raise DeployKeyRetentionAuthorityError("retention history metadata is invalid")
        sha, parents = fields[1], tuple(fields[2:])
        if (
            _COMMIT_SHA.fullmatch(sha) is None
            or any(_COMMIT_SHA.fullmatch(parent) is None for parent in parents)
            or any(len(parent) != len(sha) for parent in parents)
        ):
            raise DeployKeyRetentionAuthorityError("retention history metadata is invalid")
        try:
            committed_at = datetime.fromtimestamp(int(fields[0]), tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise DeployKeyRetentionAuthorityError(
                "retention history metadata is invalid"
            ) from None
        records.append(_HistoryRecord(sha, committed_at, parents))
    if records[0].sha != expected_head:
        raise DeployKeyRetentionAuthorityError("retention history does not match observed head")
    return tuple(records)


def _repository_url(target: str) -> str:
    owner, repository = target.split("/", 1)
    return f"ssh://git@github.com/{quote(owner, safe='')}/{quote(repository, safe='')}.git"
