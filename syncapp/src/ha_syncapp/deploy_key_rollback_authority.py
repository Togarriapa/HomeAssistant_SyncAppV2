"""Proof-bound read-only Repo B authority for deployment rollback safety."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReferenceSnapshot,
    read_repo_b_deploy_key_references,
)
from .deployment_rollback import RollbackRepositoryProof

_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class DeployKeyRollbackRepositoryAuthorityError(RuntimeError):
    """Rollback repository proof failed closed without exposing sensitive detail."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class DeployKeyRollbackRepositoryAuthority:
    """One immutable deploy-key generation authorized only for rollback ref proof."""

    proof: DeployKeyAccessProof
    key_directory: Path = field(repr=False)
    access_work_directory: Path = field(repr=False)
    known_hosts_file: Path = field(default=Path("/app/github_known_hosts"), repr=False)
    git_executable: Path = field(default=Path("/usr/bin/git"), repr=False)
    ssh_executable: Path = field(default=Path("/usr/bin/ssh"), repr=False)

    def __post_init__(self) -> None:
        paths = (
            self.key_directory,
            self.access_work_directory,
            self.known_hosts_file,
            self.git_executable,
            self.ssh_executable,
        )
        if type(self.proof) is not DeployKeyAccessProof or any(
            not isinstance(path, Path) or not path.is_absolute() for path in paths
        ):
            raise DeployKeyRollbackRepositoryAuthorityError(
                "deploy-key rollback repository authority is invalid"
            )

    def read(self, target: str, repository_id: int) -> RollbackRepositoryProof:
        """Read exactly one canonical main ref using the bound key generation."""
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
            ) from None
        except Exception:
            raise DeployKeyRollbackRepositoryAuthorityError(
                "rollback repository authority is invalid"
            ) from None

        if (
            type(snapshot) is not DeployKeyReferenceSnapshot
            or snapshot.target.casefold() != self.proof.target.casefold()
            or snapshot.repository_id != self.proof.repository_id
            or snapshot.key_fingerprint != self.proof.key_fingerprint
            or snapshot.generation_id != self.proof.generation_id
        ):
            raise DeployKeyRollbackRepositoryAuthorityError(
                "rollback repository authority is invalid"
            )
        main = tuple(ref for ref in snapshot.references if ref.name == "refs/heads/main")
        if len(main) != 1 or _COMMIT.fullmatch(main[0].commit_sha) is None:
            raise DeployKeyRollbackRepositoryAuthorityError(
                "rollback repository evidence is invalid"
            )
        proof = RollbackRepositoryProof(repository_id, True, main[0].commit_sha)
        proof.validate()
        return proof
