from __future__ import annotations

import inspect

import pytest
from ha_syncapp.live_apply_intent import LiveApplyIntent, LiveApplyIntentError


def test_live_apply_intent_is_not_publicly_constructible_from_scalar_values() -> None:
    signature = inspect.signature(LiveApplyIntent)
    assert tuple(signature.parameters) == ()

    with pytest.raises((TypeError, LiveApplyIntentError)):
        LiveApplyIntent(  # type: ignore[call-arg]
            deployment_id="00000000-0000-0000-0000-000000000000",
            target="owner/repository",
            repository_id=1,
            baseline_sha="a" * 40,
            candidate_sha="b" * 40,
            stage_manifest_sha256="c" * 64,
            backup_slug="backup",
            homeassistant_root="/homeassistant",
            operations_sha256="d" * 64,
        )


def test_live_apply_intent_is_immutable() -> None:
    assert LiveApplyIntent.__dataclass_params__.frozen is True
