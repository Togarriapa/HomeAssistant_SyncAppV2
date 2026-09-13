from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from ha_syncapp.database_history_evidence import (
    DatabaseHistoryRecord,
    TrustedDatabaseHistoryEvidence,
    validate_trusted_database_history_evidence,
)
from ha_syncapp.database_history_replace_transport import (
    DatabaseHistoryReplacementFailureKind,
    DatabaseHistoryReplacementTransportError,
)
from ha_syncapp.database_retention_work import (
    DATABASE_RETENTION_WORK_KIND,
    DatabaseRetentionWorkDisposition,
    claim_database_retention_work,
    database_retention_work_key,
    discover_database_retention_work,
    run_database_retention_work_pass,
)
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
TOKEN = "github-secret-sentinel"
NOW = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
HEAD = "1" * 40
OLD = "0" * 40


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    store.record_synchronization_baseline(
        TARGET,
        "database",
        "f" * 64,
        HEAD,
        synchronized_at=NOW - timedelta(days=1),
    )
    return store


def _evidence(*, prunable: bool = True, head: str = HEAD) -> TrustedDatabaseHistoryEvidence:
    age = 10 if prunable else 6
    return validate_trusted_database_history_evidence(
        branch_head=BranchHead(TARGET, 123, "database", head),
        records=(
            DatabaseHistoryRecord(head, NOW - timedelta(days=1), (OLD,)),
            DatabaseHistoryRecord(OLD, NOW - timedelta(days=age), ()),
        ),
        reference_time=NOW,
        retention_days=7,
    )


def test_identity_is_hash_bound_to_repository_head_and_plan_without_secrets() -> None:
    evidence = _evidence()
    first = database_retention_work_key(evidence)
    assert first == database_retention_work_key(evidence)
    assert len(first) == 64
    assert TARGET not in first
    assert TOKEN not in first
    assert first != database_retention_work_key(_evidence(prunable=False))
    assert first != database_retention_work_key(_evidence(head="2" * 40))


def test_discovery_of_same_exact_plan_converges_on_one_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    evidence = _evidence()
    fetch = Mock(return_value=evidence)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.fetch_trusted_database_history_evidence", fetch
    )
    try:
        first = discover_database_retention_work(
            store, TARGET, TOKEN, retention_days=7, reference_time=NOW
        )
        second = discover_database_retention_work(
            store, TARGET, TOKEN, retention_days=7, reference_time=NOW
        )
        claimed = claim_database_retention_work(store, now=NOW)
        assert first == second
        assert claimed is not None
        assert claimed.work_kind == DATABASE_RETENTION_WORK_KIND
        assert claim_database_retention_work(store, now=NOW) is None
    finally:
        store.__exit__(None, None, None)


def test_noop_completes_without_staging_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    evidence = _evidence(prunable=False)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.fetch_trusted_database_history_evidence",
        Mock(return_value=evidence),
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.reprove_database_history_prewrite", Mock()
    )
    authorization = Mock(requires_replacement=False)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.authorize_database_history_replacement",
        Mock(return_value=authorization),
    )
    stage = Mock()
    replace = Mock(return_value=False)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.prepare_database_history_staging", stage
    )
    monkeypatch.setattr("ha_syncapp.database_retention_work.replace_database_history", replace)
    try:
        result = run_database_retention_work_pass(
            store,
            tmp_path / "retention-staging",
            TARGET,
            TOKEN,
            retention_days=7,
            reference_time=NOW,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == "succeeded"
    assert result.processed.disposition is DatabaseRetentionWorkDisposition.NO_CHANGE
    stage.assert_not_called()
    replace.assert_called_once_with(authorization=authorization)


def test_success_updates_database_baseline_to_rebuilt_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    evidence = _evidence()
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.fetch_trusted_database_history_evidence",
        Mock(return_value=evidence),
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.reprove_database_history_prewrite", Mock()
    )
    authorization = Mock(requires_replacement=True)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.authorize_database_history_replacement",
        Mock(return_value=authorization),
    )
    repository = tmp_path / "retention-staging" / "isolated"
    repository.mkdir(parents=True)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.prepare_database_history_staging",
        Mock(return_value=repository),
    )
    artifact = Mock(replacement_head_sha="2" * 40)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.build_database_history_replacement",
        Mock(return_value=artifact),
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.replace_database_history", Mock(return_value=True)
    )
    try:
        result = run_database_retention_work_pass(
            store,
            tmp_path / "retention-staging",
            TARGET,
            TOKEN,
            retention_days=7,
            reference_time=NOW,
        )
        baseline = store.synchronization_baseline(TARGET, "database")
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.disposition is DatabaseRetentionWorkDisposition.REPLACED
    assert baseline is not None
    assert baseline.snapshot_id == "f" * 64
    assert baseline.commit_sha == "2" * 40
    assert not repository.exists()


def test_unanchored_remote_history_is_blocked_before_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    evidence = _evidence(head="2" * 40)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.fetch_trusted_database_history_evidence",
        Mock(return_value=evidence),
    )
    authorize = Mock()
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.authorize_database_history_replacement", authorize
    )
    try:
        result = run_database_retention_work_pass(
            store,
            tmp_path / "retention-staging",
            TARGET,
            TOKEN,
            retention_days=7,
            reference_time=NOW,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == "blocked"
    authorize.assert_not_called()


@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [
        (DatabaseHistoryReplacementFailureKind.TRANSIENT, "retry"),
        (DatabaseHistoryReplacementFailureKind.INVALID, "blocked"),
        (DatabaseHistoryReplacementFailureKind.STALE, "blocked"),
        (DatabaseHistoryReplacementFailureKind.REJECTED, "blocked"),
    ],
)
def test_transport_failure_classification_controls_durable_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: DatabaseHistoryReplacementFailureKind,
    expected_status: str,
) -> None:
    store = _store(tmp_path)
    evidence = _evidence()
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.fetch_trusted_database_history_evidence",
        Mock(return_value=evidence),
    )
    authorization = Mock(requires_replacement=True)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.reprove_database_history_prewrite", Mock()
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.authorize_database_history_replacement",
        Mock(return_value=authorization),
    )
    repository = tmp_path / "retention-staging" / "isolated"
    repository.mkdir(parents=True)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.prepare_database_history_staging",
        Mock(return_value=repository),
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.build_database_history_replacement", Mock()
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.replace_database_history",
        Mock(side_effect=DatabaseHistoryReplacementTransportError("sanitized", kind=kind)),
    )
    try:
        result = run_database_retention_work_pass(
            store,
            tmp_path / "retention-staging",
            TARGET,
            TOKEN,
            retention_days=7,
            reference_time=NOW,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.processed is not None
    assert result.processed.work.status == expected_status
    assert not repository.exists()


def test_interrupted_item_is_recovered_and_claimed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    evidence = _evidence(prunable=False)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.fetch_trusted_database_history_evidence",
        Mock(return_value=evidence),
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.reprove_database_history_prewrite", Mock()
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.authorize_database_history_replacement",
        Mock(return_value=Mock(requires_replacement=False)),
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.replace_database_history", Mock(return_value=False)
    )
    discover_database_retention_work(store, TARGET, TOKEN, retention_days=7, reference_time=NOW)
    running = claim_database_retention_work(store, now=NOW)
    assert running is not None and running.status == "running"
    try:
        result = run_database_retention_work_pass(
            store,
            tmp_path / "retention-staging",
            TARGET,
            TOKEN,
            retention_days=7,
            reference_time=NOW,
            recover_interrupted=True,
        )
    finally:
        store.__exit__(None, None, None)

    assert result.recovered_interrupted == 1
    assert result.processed is not None
    assert result.processed.work.status == "succeeded"
    assert result.processed.work.attempts == 2


def test_restart_recovers_exact_already_published_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    evidence = _evidence()
    fetch_evidence = Mock(return_value=evidence)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.fetch_trusted_database_history_evidence",
        fetch_evidence,
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.reprove_database_history_prewrite", Mock()
    )
    authorization = Mock(requires_replacement=True)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.authorize_database_history_replacement",
        Mock(return_value=authorization),
    )
    repository = tmp_path / "retention-staging" / "isolated"
    repository.mkdir(parents=True)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.prepare_database_history_staging",
        Mock(return_value=repository),
    )
    artifact = Mock(replacement_head_sha="2" * 40)
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.build_database_history_replacement",
        Mock(return_value=artifact),
    )
    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.replace_database_history",
        Mock(
            side_effect=DatabaseHistoryReplacementTransportError(
                "ambiguous transport", kind=DatabaseHistoryReplacementFailureKind.TRANSIENT
            )
        ),
    )
    first = run_database_retention_work_pass(
        store,
        tmp_path / "retention-staging",
        TARGET,
        TOKEN,
        retention_days=7,
        reference_time=NOW,
    )
    assert first.processed is not None and first.processed.work.status == "retry"
    intent = store.database_retention_intent(first.processed.work.work_key)
    assert intent is not None and intent.replacement_head_sha == "2" * 40

    monkeypatch.setattr(
        "ha_syncapp.database_retention_work.fetch_trusted_branch_head",
        Mock(return_value=BranchHead(TARGET, 123, "database", "2" * 40)),
    )
    fetch_evidence.reset_mock()
    try:
        recovered = run_database_retention_work_pass(
            store,
            tmp_path / "retention-staging",
            TARGET,
            TOKEN,
            retention_days=7,
            reference_time=NOW + timedelta(seconds=60),
        )
        baseline = store.synchronization_baseline(TARGET, "database")
    finally:
        store.__exit__(None, None, None)

    assert recovered.processed is not None
    assert recovered.processed.work.status == "succeeded"
    assert recovered.processed.work.attempts == 2
    assert recovered.processed.disposition is DatabaseRetentionWorkDisposition.REPLACED
    assert baseline is not None and baseline.commit_sha == "2" * 40
    fetch_evidence.assert_not_called()
