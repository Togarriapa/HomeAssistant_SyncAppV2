from __future__ import annotations

from dataclasses import replace

import pytest
from ha_syncapp.rollback_recovery_authority import (
    RECOVERY_AUTHORITY_SCHEMA_VERSION,
    RollbackRecoveryAuthority,
    RollbackRecoveryAuthorityError,
)
from test_post_deployment_assertion_observation import _ready


def test_recovery_authority_binds_exact_canonical_plan(tmp_path, monkeypatch) -> None:
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen", "switch.garage"))
    store = chain[0]
    try:
        authority = RollbackRecoveryAuthority.create(plan)
        resource = plan.automation_target.resource_target
        assert authority.schema_version == RECOVERY_AUTHORITY_SCHEMA_VERSION
        assert authority.deployment_id == resource.deployment_id
        assert authority.candidate_sha == resource.candidate_sha
        assert authority.entity_ids_json == '["light.kitchen","switch.garage"]'
        assert authority.resource_target_sha256 == resource.target_sha256
        assert authority.automation_target_sha256 == plan.automation_target.target_sha256
        assert authority.assertion_canonical_json == plan.canonical_json
        assert authority.assertion_set_sha256 == plan.assertion_set_sha256
        assert RollbackRecoveryAuthority.from_database_row(authority.database_values()) == authority
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("candidate_sha", "f" * 39),
        ("entity_ids_json", '[ "light.kitchen" ]'),
        ("resource_target_sha256", "0" * 64),
        ("automation_target_sha256", "0" * 64),
        ("assertion_set_sha256", "0" * 64),
        ("record_sha256", "0" * 64),
    ],
)
def test_recovery_authority_rejects_rebinding_or_tampering(
    tmp_path, monkeypatch, field: str, value: object
) -> None:
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    try:
        authority = RollbackRecoveryAuthority.create(plan)
        tampered = replace(authority, **{field: value})
        with pytest.raises(RollbackRecoveryAuthorityError, match="authority"):
            tampered.validate()
    finally:
        store.__exit__(None, None, None)
