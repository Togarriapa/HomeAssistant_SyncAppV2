from __future__ import annotations

from datetime import timedelta

import pytest
from ha_syncapp.deployment_finalization import (
    finalize_deployment_once,
    is_candidate_blocked_by_finalization,
    load_deployment_finalization,
)
from ha_syncapp.deployment_rollback import (
    DeploymentRollbackError,
    RollbackBackupProof,
    RollbackRepositoryProof,
    authorize_deployment_rollback_once,
    load_deployment_rollback,
)
from ha_syncapp.post_deployment_assertion_observation import (
    evaluate_post_deployment_assertions_once,
)
from test_core_health_window import START, TOKEN
from test_deployment_finalization import _successful
from test_integration_observation import FakeSession, _factory
from test_post_deployment_assertion_observation import _ready
from test_resource_availability_observation import _responses


def _failed(tmp_path, monkeypatch):
    chain, plan = _ready(tmp_path, monkeypatch, ("light.kitchen",))
    store = chain[0]
    evaluate_post_deployment_assertions_once(
        store,
        plan,
        token=TOKEN,
        observed_at=START + timedelta(seconds=307),
        session_factory=_factory(
            FakeSession(
                _responses(
                    [
                        {
                            "entity_id": "light.kitchen",
                            "state": "unavailable",
                            "attributes": {},
                        }
                    ]
                )
            )
        ),
    )
    result = finalize_deployment_once(
        store,
        plan,
        finalized_at=START + timedelta(seconds=308),
    )
    assert result.outcome == "failure"
    finalization = load_deployment_finalization(store, plan)
    assert finalization is not None
    return chain, plan, finalization


def test_exact_failed_deployment_creates_bound_rollback_intent(tmp_path, monkeypatch):
    chain, plan, finalization = _failed(tmp_path, monkeypatch)
    store = chain[0]
    prepared = store.prepared_deployment(finalization.deployment_id)
    assert prepared is not None
    reads: list[tuple[str, object]] = []

    def repository_reader(target, token, repository_id):
        assert load_deployment_rollback(store, plan) is None
        reads.append(("repository", repository_id))
        assert target == prepared.evidence.target
        assert token == TOKEN
        return RollbackRepositoryProof(repository_id, True, prepared.evidence.baseline_sha)

    def backup_reader(slug, token):
        assert load_deployment_rollback(store, plan) is None
        reads.append(("backup", slug))
        assert token == TOKEN
        return RollbackBackupProof(
            slug=slug,
            backup_type="full",
            homeassistant_version=prepared.evidence.core_version,
            includes_homeassistant=True,
            restorable=True,
        )

    try:
        result = authorize_deployment_rollback_once(
            store,
            plan,
            github_token=TOKEN,
            supervisor_token=TOKEN,
            repository_reader=repository_reader,
            backup_reader=backup_reader,
            observed_at=START + timedelta(seconds=309),
        )
        saved = load_deployment_rollback(store, plan)
        assert saved == result.intent
        assert result.status == saved.phase == "planned"
        assert result.replayed is False
        assert saved.deployment_id == finalization.deployment_id
        assert saved.target == prepared.evidence.target
        assert saved.repository_id == prepared.evidence.repository_id
        assert saved.baseline_sha == prepared.evidence.baseline_sha
        assert saved.candidate_sha == finalization.candidate_sha
        assert saved.backup_slug == finalization.backup_slug
        assert saved.finalization_sha256 == finalization.record_sha256
        assert reads == [
            ("repository", prepared.evidence.repository_id),
            ("backup", prepared.evidence.backup_slug),
        ]
        assert is_candidate_blocked_by_finalization(store, plan, finalization.candidate_sha)

        replay = authorize_deployment_rollback_once(
            store,
            plan,
            github_token=None,
            supervisor_token=None,
            repository_reader=lambda *_args: pytest.fail("replay read repository"),
            backup_reader=lambda *_args: pytest.fail("replay read backup"),
        )
        assert replay.intent == saved
        assert replay.replayed is True
    finally:
        store.__exit__(None, None, None)


def test_success_finalization_cannot_create_rollback_authority(tmp_path, monkeypatch):
    chain, plan = _successful(tmp_path, monkeypatch)
    store = chain[0]
    finalize_deployment_once(store, plan, finalized_at=START + timedelta(seconds=308))
    try:
        with pytest.raises(DeploymentRollbackError, match="failure finalization"):
            authorize_deployment_rollback_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=lambda *_args: pytest.fail("success read repository"),
                backup_reader=lambda *_args: pytest.fail("success read backup"),
            )
        assert load_deployment_rollback(store, plan) is None
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize(
    "proof",
    [
        RollbackBackupProof("other", "full", "2026.9.3", True, True),
        RollbackBackupProof("abc123", "partial", "2026.9.3", True, True),
        RollbackBackupProof("abc123", "full", "2026.9.2", True, True),
        RollbackBackupProof("abc123", "full", "2026.9.3", False, True),
        RollbackBackupProof("abc123", "full", "2026.9.3", True, False),
    ],
)
def test_missing_changed_or_unrestorable_backup_never_creates_intent(tmp_path, monkeypatch, proof):
    chain, plan, finalization = _failed(tmp_path, monkeypatch)
    store = chain[0]
    prepared = store.prepared_deployment(finalization.deployment_id)
    assert prepared is not None
    try:
        with pytest.raises(DeploymentRollbackError, match="backup proof"):
            authorize_deployment_rollback_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=lambda *_args: RollbackRepositoryProof(
                    prepared.evidence.repository_id,
                    True,
                    prepared.evidence.baseline_sha,
                ),
                backup_reader=lambda *_args: proof,
            )
        assert load_deployment_rollback(store, plan) is None
    finally:
        store.__exit__(None, None, None)


def test_repository_identity_or_main_divergence_never_creates_intent(tmp_path, monkeypatch):
    chain, plan, finalization = _failed(tmp_path, monkeypatch)
    store = chain[0]
    prepared = store.prepared_deployment(finalization.deployment_id)
    assert prepared is not None
    try:
        for proof in (
            RollbackRepositoryProof(
                prepared.evidence.repository_id, False, prepared.evidence.baseline_sha
            ),
            RollbackRepositoryProof(
                prepared.evidence.repository_id + 1, True, prepared.evidence.baseline_sha
            ),
            RollbackRepositoryProof(prepared.evidence.repository_id, True, "f" * 40),
        ):
            with pytest.raises(DeploymentRollbackError, match="repository proof"):
                authorize_deployment_rollback_once(
                    store,
                    plan,
                    github_token=TOKEN,
                    supervisor_token=TOKEN,
                    repository_reader=lambda *_args, value=proof: value,
                    backup_reader=lambda *_args: pytest.fail("invalid repository read backup"),
                )
        assert load_deployment_rollback(store, plan) is None
    finally:
        store.__exit__(None, None, None)


def test_persistence_failure_is_sanitized_and_schema_23_migrates(tmp_path, monkeypatch):
    chain, plan, finalization = _failed(tmp_path, monkeypatch)
    store = chain[0]
    root = store._root
    prepared = store.prepared_deployment(finalization.deployment_id)
    assert prepared is not None
    store._connection.execute(
        "CREATE TRIGGER reject_rollback BEFORE INSERT ON deployment_rollback "
        "BEGIN SELECT RAISE(ABORT, 'secret-storage-detail'); END"
    )
    try:
        with pytest.raises(DeploymentRollbackError, match="state is invalid") as error:
            authorize_deployment_rollback_once(
                store,
                plan,
                github_token=TOKEN,
                supervisor_token=TOKEN,
                repository_reader=lambda *_args: RollbackRepositoryProof(
                    prepared.evidence.repository_id,
                    True,
                    prepared.evidence.baseline_sha,
                ),
                backup_reader=lambda *_args: RollbackBackupProof(
                    prepared.evidence.backup_slug,
                    "full",
                    prepared.evidence.core_version,
                    True,
                    True,
                ),
            )
        assert "secret-storage-detail" not in str(error.value)
        assert load_deployment_rollback(store, plan) is None
        store._connection.execute("DROP TRIGGER reject_rollback")
        store._connection.execute("DROP TABLE deployment_rollback")
        store._connection.execute("PRAGMA user_version = 23")
        store._connection.commit()
    finally:
        store.__exit__(None, None, None)

    from ha_syncapp.state import StateStore

    with StateStore(root) as reopened:
        assert reopened._connection.execute("PRAGMA user_version").fetchone()[0] == 24
        assert reopened._connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'deployment_rollback'"
        ).fetchone() == ("deployment_rollback",)
