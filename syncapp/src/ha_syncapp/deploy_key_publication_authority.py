"""Explicit proof-bound authority for ordinary non-force Repo B publication."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .baseline_anchor import (
    BaselineAnchorError,
    anchor_trusted_baseline,
    anchor_trusted_baseline_with_deploy_key,
)
from .deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReferenceSnapshot,
    read_repo_b_deploy_key_references,
)
from .git_workspace import GitWorkspace
from .github_repo import (
    BranchAbsence,
    BranchHead,
    RepositoryVerificationError,
    fetch_optional_trusted_branch_head,
)
from .publication_intent import PublicationIntent
from .publication_result import PublicationResultError, verify_publication_result
from .publication_state import PublicationStateError, record_verified_publication
from .publication_transport import (
    PublicationTransportError,
    push_publication_intent_with_deploy_key,
)
from .publication_workflow import PublicationWorkflowError, complete_authorized_publication
from .state import StateStore, SynchronizationBaseline

_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REFERENCE = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DeployKeyPublicationAuthorityError(RuntimeError):
    """Ordinary publication authority failed closed without sensitive detail."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class TokenPublicationAuthority:
    """Compatibility adapter for the existing token-backed publication path."""

    token: str = field(repr=False)

    def observe(self, target: str, repository_id: int, branch: str) -> BranchHead | BranchAbsence:
        try:
            return fetch_optional_trusted_branch_head(
                target, self.token, expected_id=repository_id, branch=branch
            )
        except RepositoryVerificationError as exc:
            raise DeployKeyPublicationAuthorityError(
                "publication branch observation failed", transient=True
            ) from exc

    def anchor(self, workspace: GitWorkspace, remote: BranchHead) -> str:
        try:
            return anchor_trusted_baseline(workspace, remote, self.token)
        except BaselineAnchorError as exc:
            cause = exc.__cause__
            raise DeployKeyPublicationAuthorityError(
                "publication baseline acquisition failed",
                transient=isinstance(cause, DeployKeyAccessError) and cause.transient,
            ) from exc

    def complete(
        self,
        store: StateStore,
        workspace: GitWorkspace,
        intent: PublicationIntent,
        *,
        synchronized_at: datetime | None = None,
    ) -> SynchronizationBaseline:
        try:
            return complete_authorized_publication(
                store, workspace, intent, self.token, synchronized_at=synchronized_at
            )
        except PublicationWorkflowError as exc:
            raise DeployKeyPublicationAuthorityError(
                "publication workflow failed", transient=True
            ) from exc


@dataclass(frozen=True, slots=True)
class DeployKeyPublicationAuthority:
    """One immutable proof/key generation for ordinary non-force publication."""

    proof: DeployKeyAccessProof
    key_directory: Path = field(repr=False)
    access_work_directory: Path = field(repr=False)
    known_hosts_file: Path = field(default=Path("/app/github_known_hosts"), repr=False)
    git_executable: Path = field(default=Path("/usr/bin/git"), repr=False)
    ssh_executable: Path = field(default=Path("/usr/bin/ssh"), repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.proof) is not DeployKeyAccessProof
            or not isinstance(self.key_directory, Path)
            or not self.key_directory.is_absolute()
            or not isinstance(self.access_work_directory, Path)
            or not self.access_work_directory.is_absolute()
        ):
            raise DeployKeyPublicationAuthorityError("deploy-key publication authority is invalid")

    def observe(self, target: str, repository_id: int, branch: str) -> BranchHead | BranchAbsence:
        if not isinstance(branch, str) or _BRANCH.fullmatch(branch) is None:
            raise DeployKeyPublicationAuthorityError("publication branch authority is invalid")
        snapshot = self._references(target, repository_id)
        name = f"refs/heads/{branch}"
        matches = tuple(reference for reference in snapshot.references if reference.name == name)
        if len(matches) > 1 or any(
            _COMMIT.fullmatch(reference.commit_sha) is None for reference in matches
        ):
            raise DeployKeyPublicationAuthorityError("publication reference evidence is invalid")
        if not matches:
            return BranchAbsence(target, repository_id, branch)
        return BranchHead(target, repository_id, branch, matches[0].commit_sha)

    def anchor(self, workspace: GitWorkspace, remote: BranchHead) -> str:
        try:
            return anchor_trusted_baseline_with_deploy_key(
                workspace,
                remote,
                self.proof,
                self.key_directory,
                known_hosts_file=self.known_hosts_file,
                git_executable=self.git_executable,
                ssh_executable=self.ssh_executable,
            )
        except BaselineAnchorError as exc:
            raise DeployKeyPublicationAuthorityError(
                "publication baseline acquisition failed", transient=True
            ) from exc

    def complete(
        self,
        store: StateStore,
        workspace: GitWorkspace,
        intent: PublicationIntent,
        *,
        synchronized_at: datetime | None = None,
    ) -> SynchronizationBaseline:
        try:
            self.observe(intent.target, intent.repository_id, intent.branch)
            pushed = push_publication_intent_with_deploy_key(
                workspace,
                intent,
                self.proof,
                self.key_directory,
                known_hosts_file=self.known_hosts_file,
                git_executable=self.git_executable,
                ssh_executable=self.ssh_executable,
            )
            if pushed != intent.local_commit_sha:
                raise DeployKeyPublicationAuthorityError("publication result evidence is invalid")
            after = self.observe(intent.target, intent.repository_id, intent.branch)
            if isinstance(after, BranchAbsence):
                raise DeployKeyPublicationAuthorityError(
                    "publication result could not be confirmed", transient=True
                )
            result = verify_publication_result(intent, after)
            return record_verified_publication(
                store,
                workspace,
                intent,
                result,
                synchronized_at=synchronized_at,
            )
        except DeployKeyPublicationAuthorityError:
            raise
        except PublicationTransportError as exc:
            raise DeployKeyPublicationAuthorityError(
                "publication transport failed", transient=exc.transient
            ) from exc
        except (PublicationResultError, PublicationStateError) as exc:
            raise DeployKeyPublicationAuthorityError("publication workflow failed") from exc

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
            raise DeployKeyPublicationAuthorityError(
                "publication reference transport failed"
                if exc.transient
                else "publication reference authority is invalid",
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
            raise DeployKeyPublicationAuthorityError("publication reference evidence is invalid")
        previous: str | None = None
        for reference in snapshot.references:
            if (
                not isinstance(reference.name, str)
                or _REFERENCE.fullmatch(reference.name) is None
                or reference.name.endswith(("/", ".", ".lock"))
                or ".." in reference.name
                or "//" in reference.name
                or "@{" in reference.name
                or any(
                    character in " ~^:?*[\\" or ord(character) < 0x20
                    for character in reference.name
                )
                or not isinstance(reference.commit_sha, str)
                or _COMMIT.fullmatch(reference.commit_sha) is None
                or (previous is not None and reference.name <= previous)
            ):
                raise DeployKeyPublicationAuthorityError(
                    "publication reference evidence is invalid"
                )
            previous = reference.name
        return snapshot


PublicationAuthority = TokenPublicationAuthority | DeployKeyPublicationAuthority
PublicationCredential = str | DeployKeyPublicationAuthority


def resolve_publication_authority(
    authority: DeployKeyPublicationAuthority | None = None,
    *,
    token: str | None = None,
) -> PublicationAuthority:
    """Require exactly one explicit transport authority."""
    if (authority is None) == (token is None):
        raise DeployKeyPublicationAuthorityError(
            "publication requires exactly one transport authority"
        )
    if authority is not None:
        if type(authority) is not DeployKeyPublicationAuthority:
            raise DeployKeyPublicationAuthorityError("publication authority is invalid")
        return authority
    if not isinstance(token, str) or not token:
        raise DeployKeyPublicationAuthorityError("publication token authority is invalid")
    return TokenPublicationAuthority(token)
