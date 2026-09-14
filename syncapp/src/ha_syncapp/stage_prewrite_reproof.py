"""Re-prove isolated candidate Stage bytes before deployment mutation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from .apply_authorization import ApplyAuthorization
from .candidate_stage import CandidateStage, verify_candidate_stage


class StagePrewriteReproofError(RuntimeError):
    """The authorized candidate Stage could not be re-proven safely."""


_PREWRITE_PRODUCER = object()


@dataclass(frozen=True, slots=True, init=False)
class StagePrewriteEvidence:
    """Immutable ephemeral proof for one exact re-verified Stage tree."""

    deployment_id: str
    target: str
    repository_id: int
    candidate_sha: str
    stage_manifest_sha256: str

    def __init__(
        self,
        *,
        deployment_id: str,
        target: str,
        repository_id: int,
        candidate_sha: str,
        stage_manifest_sha256: str,
        _producer: object | None = None,
    ) -> None:
        if _producer is not _PREWRITE_PRODUCER:
            _reject("Stage pre-write evidence must be created by the trusted producer")
        object.__setattr__(self, "deployment_id", deployment_id)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "repository_id", repository_id)
        object.__setattr__(self, "candidate_sha", candidate_sha)
        object.__setattr__(self, "stage_manifest_sha256", stage_manifest_sha256)

    @classmethod
    def _from_authorization(
        cls,
        authorization: ApplyAuthorization,
    ) -> StagePrewriteEvidence:
        return cls(
            deployment_id=authorization.deployment_id,
            target=authorization.target,
            repository_id=authorization.repository_id,
            candidate_sha=authorization.candidate_sha,
            stage_manifest_sha256=authorization.stage_manifest_sha256,
            _producer=_PREWRITE_PRODUCER,
        )


def reprove_stage_for_apply(
    authorization: ApplyAuthorization,
    stage: CandidateStage,
) -> StagePrewriteEvidence:
    """Re-verify exact isolated Stage bytes without mutating Home Assistant."""
    if type(authorization) is not ApplyAuthorization:
        _reject("Apply authorization evidence is invalid")
    if type(stage) is not CandidateStage:
        _reject("candidate Stage evidence is invalid")

    authorized_before = _authorization_binding(authorization)
    stage_before = _stage_binding(stage)
    if stage_before != authorized_before[1:] or stage.branch != "candidate":
        _reject("candidate Stage binding does not match Apply authorization")

    try:
        verify_candidate_stage(stage)
    except Exception:
        _reject("candidate Stage integrity re-verification failed")

    if _authorization_binding(authorization) != authorized_before:
        _reject("Apply authorization changed during verification")
    if _stage_binding(stage) != stage_before or stage.branch != "candidate":
        _reject("candidate Stage binding changed during verification")

    return StagePrewriteEvidence._from_authorization(authorization)


def _authorization_binding(
    authorization: ApplyAuthorization,
) -> tuple[str, str, int, str, str]:
    return (
        authorization.deployment_id,
        authorization.target,
        authorization.repository_id,
        authorization.candidate_sha,
        authorization.stage_manifest_sha256,
    )


def _stage_binding(stage: CandidateStage) -> tuple[str, int, str, str]:
    return (
        stage.target,
        stage.repository_id,
        stage.commit_sha,
        stage.manifest_sha256,
    )


def _reject(message: str) -> NoReturn:
    raise StagePrewriteReproofError(message) from None
