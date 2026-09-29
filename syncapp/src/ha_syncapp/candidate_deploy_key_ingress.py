"""Explicit deploy-key authority for candidate observation and fetch."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .candidate_detection import CandidateObservation
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
    read_repo_b_deploy_key_references,
)

_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class CandidateDeployKeyIngressError(RuntimeError):
    """Candidate ingress authority or transport failed closed."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class DeployKeyCandidateIngress:
    """One exact proof/key-generation binding for candidate-only Git operations."""

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
            or not isinstance(self.access_work_directory, Path)
            or not isinstance(self.known_hosts_file, Path)
            or not isinstance(self.git_executable, Path)
            or not isinstance(self.ssh_executable, Path)
        ):
            raise CandidateDeployKeyIngressError("candidate deploy-key authority is invalid")

    def observe(self, target: str, *, expected_id: int) -> CandidateObservation:
        """Observe only the exact candidate branch with this proof/key generation."""
        if (
            target != self.proof.target
            or type(expected_id) is not int
            or expected_id != self.proof.repository_id
        ):
            raise CandidateDeployKeyIngressError("candidate deploy-key authority is invalid")
        try:
            snapshot = read_repo_b_deploy_key_references(
                self.proof,
                target,
                expected_id,
                self.key_directory,
                known_hosts_file=self.known_hosts_file,
                work_directory=self.access_work_directory,
                git_executable=self.git_executable,
                ssh_executable=self.ssh_executable,
            )
        except DeployKeyAccessError as error:
            raise CandidateDeployKeyIngressError(
                "candidate deploy-key transport is temporarily unavailable"
                if error.transient
                else "candidate deploy-key authority is invalid",
                transient=error.transient,
            ) from None
        self._validate_snapshot(snapshot, target, expected_id)
        candidates = tuple(
            reference
            for reference in snapshot.references
            if reference.name == "refs/heads/candidate"
        )
        if len(candidates) > 1:
            raise CandidateDeployKeyIngressError("candidate deploy-key evidence is invalid")
        commit_sha = None if not candidates else candidates[0].commit_sha
        if commit_sha is not None and _COMMIT_SHA.fullmatch(commit_sha) is None:
            raise CandidateDeployKeyIngressError("candidate deploy-key evidence is invalid")
        return CandidateObservation(target, expected_id, "candidate", commit_sha)

    def fetch(
        self,
        observation: CandidateObservation,
        expected_sha: str,
        workspace_root: Path,
        home_assistant_root: Path,
    ) -> CandidateFetch:
        """Fetch one exact observed commit without token transport or fallback."""
        if (
            type(observation) is not CandidateObservation
            or observation.target != self.proof.target
            or observation.repository_id != self.proof.repository_id
            or observation.branch != "candidate"
            or observation.commit_sha != expected_sha
            or _COMMIT_SHA.fullmatch(expected_sha) is None
        ):
            raise CandidateDeployKeyIngressError("candidate deploy-key evidence is invalid")
        try:
            return fetch_trusted_candidate_with_deploy_key(
                self.proof,
                observation.target,
                observation.repository_id,
                expected_sha,
                self.key_directory,
                workspace_root,
                home_assistant_root,
                known_hosts_file=self.known_hosts_file,
                git_executable=self.git_executable,
                ssh_executable=self.ssh_executable,
            )
        except CandidateFetchError as error:
            raise CandidateDeployKeyIngressError(
                "candidate deploy-key transport is temporarily unavailable"
                if error.transient
                else "candidate deploy-key evidence is invalid",
                transient=error.transient,
            ) from None

    def _validate_snapshot(
        self,
        snapshot: DeployKeyReferenceSnapshot,
        target: str,
        expected_id: int,
    ) -> None:
        if (
            type(snapshot) is not DeployKeyReferenceSnapshot
            or snapshot.target != target
            or snapshot.repository_id != expected_id
            or snapshot.key_fingerprint != self.proof.key_fingerprint
            or snapshot.generation_id != self.proof.generation_id
            or any(type(reference) is not DeployKeyReference for reference in snapshot.references)
        ):
            raise CandidateDeployKeyIngressError("candidate deploy-key evidence is invalid")
