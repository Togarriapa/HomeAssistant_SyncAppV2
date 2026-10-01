"""Proof-bound read-only Repo B authority for deployment rollback safety."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReferenceSnapshot,
    read_repo_b_deploy_key_references,
)

if TYPE_CHECKING:
    from .deployment_rollback import RollbackRepositoryProof

_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REFERENCE = re.compile(r"^refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DeployKeyRollbackRepositoryAuthorityError(RuntimeError):
    """Rollback repository authority failed closed without sensitive detail."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class DeployKeyRollbackRepositoryAuthority:
    """One immutable key generation authorized only to prove Repo B refs."""

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
            raise DeployKeyRollbackRepositoryAuthorityError(
                "rollback deploy-key authority is invalid"
            )

    def read(self, target: str, repository_id: int) -> RollbackRepositoryProof:
        """Return the exact protected main head from canonical SSH evidence."""
        snapshot = self._references(target, repository_id)
        matches = tuple(
            reference for reference in snapshot.references if reference.name == "refs/heads/main"
        )
        if len(matches) != 1 or _COMMIT.fullmatch(matches[0].commit_sha) is None:
            raise DeployKeyRollbackRepositoryAuthorityError(
                "rollback repository reference evidence is invalid"
            )
        from .deployment_rollback import RollbackRepositoryProof

        return RollbackRepositoryProof(repository_id, True, matches[0].commit_sha)

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
            raise DeployKeyRollbackRepositoryAuthorityError(
                "rollback repository transport failed"
                if exc.transient
                else "rollback repository authority is invalid",
                transient=exc.transient,
            ) from exc
        if (
            type(snapshot) is not DeployKeyReferenceSnapshot
            or self.proof.target.casefold() != target.casefold()
            or self.proof.repository_id != repository_id
            or snapshot.target.casefold() != target.casefold()
            or snapshot.repository_id != repository_id
            or snapshot.key_fingerprint != self.proof.key_fingerprint
            or snapshot.generation_id != self.proof.generation_id
            or _SHA256.fullmatch(snapshot.observation_sha256) is None
            or type(snapshot.references) is not tuple
        ):
            raise DeployKeyRollbackRepositoryAuthorityError(
                "rollback repository reference evidence is invalid"
            )
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
                raise DeployKeyRollbackRepositoryAuthorityError(
                    "rollback repository reference evidence is invalid"
                )
            previous = reference.name
        return snapshot
