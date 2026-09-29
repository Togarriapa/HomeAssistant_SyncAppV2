"""Explicit proof-bound authority for candidate promotion publication."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .deploy_key_access import DeployKeyAccessProof
from .deployment_promotion import DeploymentPromotion, PromotionRemoteState
from .deployment_promotion_transport import (
    DeploymentPromotionTransportError,
    publish_promotion_refs_with_deploy_key,
    read_promotion_remote_state_with_deploy_key,
)


class DeployKeyPromotionAuthorityError(RuntimeError):
    """Promotion authority failed closed without sensitive detail."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class PromotionAuthority(Protocol):
    """One complete and non-mixable promotion transport authority."""

    def read(self, intent: DeploymentPromotion) -> PromotionRemoteState: ...

    def publish(self, intent: DeploymentPromotion, state: PromotionRemoteState) -> None: ...


@dataclass(frozen=True, slots=True)
class DeployKeyPromotionAuthority:
    """One immutable proof/key generation for candidate promotion."""

    proof: DeployKeyAccessProof
    key_directory: Path = field(repr=False)
    access_work_directory: Path = field(repr=False)
    workspace_root: Path = field(repr=False)
    home_assistant_root: Path = field(repr=False)
    known_hosts_file: Path = field(default=Path("/app/github_known_hosts"), repr=False)
    git_executable: Path = field(default=Path("/usr/bin/git"), repr=False)
    ssh_executable: Path = field(default=Path("/usr/bin/ssh"), repr=False)

    def __post_init__(self) -> None:
        paths = (
            self.key_directory,
            self.access_work_directory,
            self.workspace_root,
            self.home_assistant_root,
            self.known_hosts_file,
            self.git_executable,
            self.ssh_executable,
        )
        if type(self.proof) is not DeployKeyAccessProof or any(
            not isinstance(path, Path) or not path.is_absolute() for path in paths
        ):
            raise DeployKeyPromotionAuthorityError("deploy-key promotion authority is invalid")

    def read(self, intent: DeploymentPromotion) -> PromotionRemoteState:
        try:
            return read_promotion_remote_state_with_deploy_key(
                self.proof,
                intent.target,
                intent.repository_id,
                intent.known_good_tag,
                self.key_directory,
                work_directory=self.access_work_directory,
                known_hosts_file=self.known_hosts_file,
                git_executable=self.git_executable,
                ssh_executable=self.ssh_executable,
            )
        except DeploymentPromotionTransportError as exc:
            raise DeployKeyPromotionAuthorityError(
                "promotion reference transport failed"
                if exc.transient
                else "promotion reference authority is invalid",
                transient=exc.transient,
            ) from exc
        except Exception as exc:
            raise DeployKeyPromotionAuthorityError(
                "promotion reference authority is invalid"
            ) from exc

    def publish(self, intent: DeploymentPromotion, state: PromotionRemoteState) -> None:
        try:
            publish_promotion_refs_with_deploy_key(
                intent,
                state,
                self.proof,
                self.key_directory,
                self.workspace_root,
                self.home_assistant_root,
                known_hosts_file=self.known_hosts_file,
                git_executable=self.git_executable,
                ssh_executable=self.ssh_executable,
            )
        except DeploymentPromotionTransportError as exc:
            raise DeployKeyPromotionAuthorityError(
                "promotion publication transport failed"
                if exc.transient
                else "promotion publication authority is invalid",
                transient=exc.transient,
            ) from exc
        except Exception as exc:
            raise DeployKeyPromotionAuthorityError(
                "promotion publication authority is invalid"
            ) from exc
