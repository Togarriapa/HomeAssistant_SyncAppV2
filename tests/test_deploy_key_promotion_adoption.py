from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp import deploy_key_promotion_authority as authority_module
from ha_syncapp.candidate_promotion_execution import execute_candidate_promotion_once
from ha_syncapp.config import Config
from ha_syncapp.deploy_key_access import DeployKeyAccessError, DeployKeyAccessProof
from ha_syncapp.deployment_promotion import PromotionRemoteState
from ha_syncapp.deployment_promotion_transport import DeploymentPromotionTransportError
from ha_syncapp.state import StateStore
from test_core_health_window import START
from test_deployment_promotion import _ready

TARGET = "Owner/Home"
REPOSITORY_ID = 123
BASELINE = "a" * 40
CANDIDATE = "b" * 40
NOW = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)
PROOF = DeployKeyAccessProof(
    target=TARGET,
    repository_id=REPOSITORY_ID,
    key_fingerprint="SHA256:" + "A" * 43,
    generation_id="123e4567-e89b-42d3-a456-426614174000",
    ref_count=2,
    observation_sha256="c" * 64,
)


def _authority(tmp_path: Path) -> authority_module.DeployKeyPromotionAuthority:
    return authority_module.DeployKeyPromotionAuthority(
        PROOF,
        tmp_path / "key",
        tmp_path / "access",
        tmp_path / "promotion",
        tmp_path / "homeassistant",
    )


def test_authority_reads_and_publishes_only_through_proof_bound_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _authority(tmp_path)
    state = PromotionRemoteState(CANDIDATE, BASELINE, None)
    observed: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def read(*args: object, **kwargs: object) -> PromotionRemoteState:
        observed.append(("read", args, kwargs))
        return state

    def publish(*args: object, **kwargs: object) -> None:
        observed.append(("publish", args, kwargs))

    monkeypatch.setattr(authority_module, "read_promotion_remote_state_with_deploy_key", read)
    monkeypatch.setattr(authority_module, "publish_promotion_refs_with_deploy_key", publish)
    intent = type("Intent", (), {"target": TARGET, "repository_id": REPOSITORY_ID,
                                  "known_good_tag": "syncapp-known-good-123e4567-e89b-42d3-a456-426614174000"})()

    assert authority.read(intent) is state
    authority.publish(intent, state)

    assert observed[0][1][:5] == (
        PROOF, TARGET, REPOSITORY_ID, intent.known_good_tag, tmp_path / "key"
    )
    assert observed[1][1][:6] == (
        intent, state, PROOF, tmp_path / "key", tmp_path / "promotion", tmp_path / "homeassistant"
    )
    rendered = repr(authority)
    assert str(tmp_path) not in rendered
    assert "token" not in repr(observed)


@pytest.mark.parametrize("transient", [False, True])
def test_authority_sanitizes_and_preserves_failure_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient: bool
) -> None:
    authority = _authority(tmp_path)
    monkeypatch.setattr(
        authority_module,
        "read_promotion_remote_state_with_deploy_key",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DeploymentPromotionTransportError("private-key secret detail", transient=transient)
        ),
    )
    intent = type("Intent", (), {"target": TARGET, "repository_id": REPOSITORY_ID,
                                  "known_good_tag": "syncapp-known-good-123e4567-e89b-42d3-a456-426614174000"})()

    with pytest.raises(authority_module.DeployKeyPromotionAuthorityError) as caught:
        authority.read(intent)

    assert caught.value.transient is transient
    assert "secret" not in str(caught.value)


def test_candidate_execution_uses_deploy_key_authority_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, plan, baseline, candidate = _ready(tmp_path, monkeypatch)
    from ha_syncapp import candidate_promotion_execution as execution

    monkeypatch.setattr(execution, "_load_plan", lambda *_args: plan)
    now = START + timedelta(seconds=309)
    deployment_id = plan.automation_target.resource_target.deployment_id
    store.enqueue_work("candidate_promote", deployment_id, now=now)
    item = store.claim_work_kind("candidate_promote", now=now)
    assert item is not None
    states = iter(
        (
            PromotionRemoteState(candidate, baseline, None),
            PromotionRemoteState(candidate, candidate, candidate),
        )
    )
    calls: list[str] = []
    authority = _authority(tmp_path)
    monkeypatch.setattr(authority, "read", lambda _intent: calls.append("read") or next(states))
    monkeypatch.setattr(authority, "publish", lambda _intent, _state: calls.append("publish"))
    try:
        result = execute_candidate_promotion_once(
            store, item, token=None, promotion_authority=authority, now=now
        )
        assert result.status == "completed"
        assert calls == ["read", "publish", "read"]
    finally:
        store.__exit__(None, None, None)


def test_service_builds_promotion_authority_only_after_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    key = data / "syncapp" / "repo-b-deploy-key"
    key.mkdir(parents=True, mode=0o700)
    home = tmp_path / "homeassistant"
    home.mkdir()
    config = Config(
        repo_b=TARGET,
        github_token="rest-identity-token",
        repo_b_promotion_transport="deploy_key",
    )
    monkeypatch.setattr(service, "test_repo_b_deploy_key_access", lambda *_a, **_k: PROOF)
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        with pytest.raises(service.RetriggerCycleError, match="initialized"):
            service._deploy_key_promotion_authority_if_configured(store, config, data, home)
        store.record_synchronization_baseline(
            TARGET, "main", "e" * 64, BASELINE, synchronized_at=NOW
        )
        authority = service._deploy_key_promotion_authority_if_configured(
            store, config, data, home
        )

    assert authority is not None
    assert authority.home_assistant_root == home.resolve()
    assert "rest-identity-token" not in repr(authority)


def test_service_sanitizes_promotion_authority_activation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    (data / "syncapp" / "repo-b-deploy-key").mkdir(parents=True, mode=0o700)
    home = tmp_path / "homeassistant"
    home.mkdir()
    config = Config(repo_b=TARGET, github_token="rest-token",
                    repo_b_promotion_transport="deploy_key")
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        store.record_synchronization_baseline(
            TARGET, "main", "e" * 64, BASELINE, synchronized_at=NOW
        )
        monkeypatch.setattr(
            service,
            "test_repo_b_deploy_key_access",
            lambda *_a, **_k: (_ for _ in ()).throw(
                DeployKeyAccessError("rest-token private path")
            ),
        )
        with pytest.raises(service.RetriggerCycleError, match="authority is unavailable") as caught:
            service._deploy_key_promotion_authority_if_configured(store, config, data, home)
    assert "rest-token" not in str(caught.value)
