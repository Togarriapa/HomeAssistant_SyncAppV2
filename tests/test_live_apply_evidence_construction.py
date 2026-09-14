import inspect

import pytest
from ha_syncapp.live_apply_plan import LiveApplyPlan
from ha_syncapp.live_apply_preconditions import LiveApplyPreconditionEvidence


def test_live_apply_plan_is_not_publicly_constructible_from_scalar_values() -> None:
    assert tuple(inspect.signature(LiveApplyPlan).parameters) == ()
    with pytest.raises(TypeError):
        LiveApplyPlan(  # type: ignore[call-arg]
            deployment_id="deploy-1",
            target="owner/private-repo",
            repository_id=123,
            baseline_sha="1" * 40,
            candidate_sha="2" * 40,
            stage_manifest_sha256="3" * 64,
            operations=(),
        )


def test_live_apply_preconditions_are_not_publicly_constructible_from_scalars() -> None:
    assert tuple(inspect.signature(LiveApplyPreconditionEvidence).parameters) == ()
    with pytest.raises(TypeError):
        LiveApplyPreconditionEvidence(  # type: ignore[call-arg]
            deployment_id="deploy-1",
            target="owner/private-repo",
            repository_id=123,
            baseline_sha="1" * 40,
            candidate_sha="2" * 40,
            stage_manifest_sha256="3" * 64,
            root="/homeassistant",
            verified_paths=(),
        )
