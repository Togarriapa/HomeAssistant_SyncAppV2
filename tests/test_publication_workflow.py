from datetime import UTC, datetime
from pathlib import Path

import ha_syncapp.publication_workflow as workflow_module
import pytest
from ha_syncapp.git_workspace import GitWorkspace, prepare_git_workspace
from ha_syncapp.github_repo import (
    BranchAbsence,
    BranchHead,
    RepositoryVerificationError,
)
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.publication_state import PublicationStateError
from ha_syncapp.publication_workflow import PublicationWorkflowError, complete_authorized_publication
from ha_syncapp.snapshot import capture_snapshot
from ha_syncapp.state import StateStore, SynchronizationBaseline

BASELINE = "a" * 40
LOCAL = "b" * 40
TOKEN = "github-token-value"
WHEN = datetime(2026, 9, 9, 20, 45, tzinfo=UTC)


def _workspace(tmp_path: Path) -> GitWorkspace:
    source = tmp_path / "ha"
    snapshots = tmp_path / "snapshots"
    workspaces = tmp_path / "workspaces"
    source.mkdir()
    snapshots.mkdir()
    workspaces.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    snapshot = capture_snapshot(source, snapshots)
    return prepare_git_workspace(snapshot.root, workspaces)


def _intent(*, initialize: bool = False) -> PublicationIntent:
    return PublicationIntent(
        target="Owner/Home",
        repository_id=42,
        branch="main",
        local_commit_sha=LOCAL,
        expected_remote_commit_sha=None if initialize else BASELINE,
        expect_remote_absent=initialize,
    )


def _baseline(workspace: GitWorkspace) -> SynchronizationBaseline:
    return SynchronizationBaseline(
        target="Owner/Home",
        branch="main",
        snapshot_id=workspace.snapshot_id,
        commit_sha=LOCAL,
        synchronized_at=WHEN,
    )


def test_verified_publication_orders_fresh_remote_transport_and_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    intent = _intent()
    expected = _baseline(workspace)
    calls: list[str] = []

    def before(*_args: object, **_kwargs: object) -> BranchHead:
        calls.append("before")
        return BranchHead("Owner/Home", 42, "main", BASELINE)

    def push(*_args: object, **_kwargs: object) -> str:
        calls.append("push")
        return LOCAL

    def after(*_args: object, **_kwargs: object) -> BranchHead:
        calls.append("after")
        return BranchHead("Owner/Home", 42, "main", LOCAL)

    original_verify = workflow_module.verify_publication_result

    def verify(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append("verify")
        return original_verify(*args, **kwargs)

    def persist(*_args: object, **kwargs: object) -> SynchronizationBaseline:
        calls.append("persist")
        assert kwargs["synchronized_at"] == WHEN
        return expected

    monkeypatch.setattr(workflow_module, "fetch_optional_trusted_branch_head", before)
    monkeypatch.setattr(workflow_module, "push_publication_intent", push)
    monkeypatch.setattr(workflow_module, "fetch_trusted_branch_head", after)
    monkeypatch.setattr(workflow_module, "verify_publication_result", verify)
    monkeypatch.setattr(workflow_module, "record_verified_publication", persist)

    with StateStore(tmp_path / "state") as store:
        result = complete_authorized_publication(
            store,
            workspace,
            intent,
            TOKEN,
            synchronized_at=WHEN,
        )

    assert result == expected
    assert calls == ["before", "push", "after", "verify", "persist"]


def test_initial_publication_accepts_fresh_identity_bound_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    intent = _intent(initialize=True)
    expected = _baseline(workspace)

    monkeypatch.setattr(
        workflow_module,
        "fetch_optional_trusted_branch_head",
        lambda *_args, **_kwargs: BranchAbsence("Owner/Home", 42, "main"),
    )
    monkeypatch.setattr(
        workflow_module,
        "push_publication_intent",
        lambda *_args, **_kwargs: LOCAL,
    )
    monkeypatch.setattr(
        workflow_module,
        "fetch_trusted_branch_head",
        lambda *_args, **_kwargs: BranchHead("Owner/Home", 42, "main", LOCAL),
    )
    monkeypatch.setattr(
        workflow_module,
        "record_verified_publication",
        lambda *_args, **_kwargs: expected,
    )

    with StateStore(tmp_path / "state") as store:
        assert complete_authorized_publication(store, workspace, intent, TOKEN) == expected


def test_pre_transport_remote_failure_never_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    intent = _intent()
    pushed = False

    def fail_before(*_args: object, **_kwargs: object) -> BranchHead:
        raise RepositoryVerificationError("simulated outage")

    def unexpected_push(*_args: object, **_kwargs: object) -> str:
        nonlocal pushed
        pushed = True
        return LOCAL

    monkeypatch.setattr(workflow_module, "fetch_optional_trusted_branch_head", fail_before)
    monkeypatch.setattr(workflow_module, "push_publication_intent", unexpected_push)

    with StateStore(tmp_path / "state") as store:
        with pytest.raises(PublicationWorkflowError, match="every verification gate"):
            complete_authorized_publication(store, workspace, intent, TOKEN)

    assert not pushed


def test_wrong_post_push_sha_never_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    intent = _intent()
    persisted = False

    monkeypatch.setattr(
        workflow_module,
        "fetch_optional_trusted_branch_head",
        lambda *_args, **_kwargs: BranchHead("Owner/Home", 42, "main", BASELINE),
    )
    monkeypatch.setattr(
        workflow_module,
        "push_publication_intent",
        lambda *_args, **_kwargs: LOCAL,
    )
    monkeypatch.setattr(
        workflow_module,
        "fetch_trusted_branch_head",
        lambda *_args, **_kwargs: BranchHead("Owner/Home", 42, "main", "c" * 40),
    )

    def unexpected_persist(*_args: object, **_kwargs: object) -> SynchronizationBaseline:
        nonlocal persisted
        persisted = True
        return _baseline(workspace)

    monkeypatch.setattr(workflow_module, "record_verified_publication", unexpected_persist)

    with StateStore(tmp_path / "state") as store:
        with pytest.raises(PublicationWorkflowError, match="every verification gate"):
            complete_authorized_publication(store, workspace, intent, TOKEN)

    assert not persisted


def test_post_push_remote_outage_never_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    intent = _intent()
    persisted = False

    monkeypatch.setattr(
        workflow_module,
        "fetch_optional_trusted_branch_head",
        lambda *_args, **_kwargs: BranchHead("Owner/Home", 42, "main", BASELINE),
    )
    monkeypatch.setattr(
        workflow_module,
        "push_publication_intent",
        lambda *_args, **_kwargs: LOCAL,
    )

    def fail_after(*_args: object, **_kwargs: object) -> BranchHead:
        raise RepositoryVerificationError("simulated post-push outage")

    def unexpected_persist(*_args: object, **_kwargs: object) -> SynchronizationBaseline:
        nonlocal persisted
        persisted = True
        return _baseline(workspace)

    monkeypatch.setattr(workflow_module, "fetch_trusted_branch_head", fail_after)
    monkeypatch.setattr(workflow_module, "record_verified_publication", unexpected_persist)

    with StateStore(tmp_path / "state") as store:
        with pytest.raises(PublicationWorkflowError, match="every verification gate"):
            complete_authorized_publication(store, workspace, intent, TOKEN)

    assert not persisted


def test_persistence_failure_is_reported_as_incomplete_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    intent = _intent()

    monkeypatch.setattr(
        workflow_module,
        "fetch_optional_trusted_branch_head",
        lambda *_args, **_kwargs: BranchHead("Owner/Home", 42, "main", BASELINE),
    )
    monkeypatch.setattr(
        workflow_module,
        "push_publication_intent",
        lambda *_args, **_kwargs: LOCAL,
    )
    monkeypatch.setattr(
        workflow_module,
        "fetch_trusted_branch_head",
        lambda *_args, **_kwargs: BranchHead("Owner/Home", 42, "main", LOCAL),
    )

    def fail_persist(*_args: object, **_kwargs: object) -> SynchronizationBaseline:
        raise PublicationStateError("simulated persistence failure")

    monkeypatch.setattr(workflow_module, "record_verified_publication", fail_persist)

    with StateStore(tmp_path / "state") as store:
        with pytest.raises(PublicationWorkflowError, match="every verification gate"):
            complete_authorized_publication(store, workspace, intent, TOKEN)


def test_unexpected_transport_commit_evidence_blocks_post_push_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    intent = _intent()
    inspected_after = False

    monkeypatch.setattr(
        workflow_module,
        "fetch_optional_trusted_branch_head",
        lambda *_args, **_kwargs: BranchHead("Owner/Home", 42, "main", BASELINE),
    )
    monkeypatch.setattr(
        workflow_module,
        "push_publication_intent",
        lambda *_args, **_kwargs: "c" * 40,
    )

    def unexpected_after(*_args: object, **_kwargs: object) -> BranchHead:
        nonlocal inspected_after
        inspected_after = True
        return BranchHead("Owner/Home", 42, "main", LOCAL)

    monkeypatch.setattr(workflow_module, "fetch_trusted_branch_head", unexpected_after)

    with StateStore(tmp_path / "state") as store:
        with pytest.raises(PublicationWorkflowError, match="unexpected commit evidence"):
            complete_authorized_publication(store, workspace, intent, TOKEN)

    assert not inspected_after
