from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ha_syncapp.apply_authorization import authorize_candidate_apply
from ha_syncapp.candidate_backup import CandidateBackupEvidence
from ha_syncapp.candidate_changes import CandidateChange, CandidateChanges
from ha_syncapp.candidate_stage import CandidateStage, CandidateStageEntry
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.live_apply_plan import LiveApplyPlanError, build_live_apply_plan
from ha_syncapp.preapply_freshness import reprove_preapply_repo_heads
from ha_syncapp.prepared_deployment import PreparedDeployment
from ha_syncapp.stage_prewrite_reproof import reprove_stage_for_apply


def _backup() -> CandidateBackupEvidence:
    return CandidateBackupEvidence(
        target="owner/private-repo",
        repository_id=12345,
        baseline_sha="a" * 40,
        candidate_sha="b" * 40,
        stage_manifest_sha256="c" * 64,
        runtime_sha256="d" * 64,
        risk_level="high",
        core_version="2026.9.1",
        backup_slug="backup_123",
    )


def _prepared() -> PreparedDeployment:
    return PreparedDeployment(str(uuid4()), _backup(), datetime.now(UTC))


def _authorization(prepared: PreparedDeployment):
    backup = prepared.evidence

    def fetcher(target, token, *, expected_id, branch="main"):
        sha = backup.baseline_sha if branch == "main" else backup.candidate_sha
        return BranchHead(target, expected_id, branch, sha)

    freshness = reprove_preapply_repo_heads(
        prepared,
        backup,
        token="secret-token",
        head_fetcher=fetcher,
    )
    return authorize_candidate_apply(prepared, backup, freshness)


def _stage(tmp_path: Path, prepared: PreparedDeployment) -> CandidateStage:
    backup = prepared.evidence
    root = tmp_path / "candidate-stage"
    return CandidateStage(
        root=root,
        tree=root / "tree",
        manifest=root / "manifest.json",
        manifest_sha256=backup.stage_manifest_sha256,
        target=backup.target,
        repository_id=backup.repository_id,
        branch="candidate",
        commit_sha=backup.candidate_sha,
        entries=(
            CandidateStageEntry(
                path="automations.yaml",
                git_mode="100644",
                object_id="1" * 40,
                size=12,
                sha256="1" * 64,
            ),
            CandidateStageEntry(
                path="scripts/new.yaml",
                git_mode="100755",
                object_id="2" * 40,
                size=24,
                sha256="2" * 64,
            ),
        ),
    )


def _changes(prepared: PreparedDeployment) -> CandidateChanges:
    backup = prepared.evidence
    return CandidateChanges(
        target=backup.target,
        repository_id=backup.repository_id,
        baseline_sha=backup.baseline_sha,
        candidate_sha=backup.candidate_sha,
        changes=(
            CandidateChange(
                path="automations.yaml",
                status="modified",
                baseline_mode="100644",
                baseline_object_id="3" * 40,
                candidate_mode="100644",
                candidate_object_id="1" * 40,
            ),
            CandidateChange(
                path="old.yaml",
                status="deleted",
                baseline_mode="100644",
                baseline_object_id="4" * 40,
                candidate_mode=None,
                candidate_object_id=None,
            ),
            CandidateChange(
                path="scripts/new.yaml",
                status="added",
                baseline_mode=None,
                baseline_object_id=None,
                candidate_mode="100755",
                candidate_object_id="2" * 40,
            ),
        ),
    )


def _evidence(tmp_path, monkeypatch, prepared, stage):
    monkeypatch.setattr(
        "ha_syncapp.stage_prewrite_reproof.verify_candidate_stage",
        lambda _stage: None,
    )
    return reprove_stage_for_apply(_authorization(prepared), stage)


def test_plan_is_byte_sorted_immutable_and_reverifies_exact_stage(
    tmp_path,
    monkeypatch,
):
    prepared = _prepared()
    stage = _stage(tmp_path, prepared)
    evidence = _evidence(tmp_path, monkeypatch, prepared, stage)
    changes = _changes(prepared)
    verified = []

    monkeypatch.setattr(
        "ha_syncapp.live_apply_plan.verify_candidate_stage",
        lambda value: verified.append(value),
    )

    result = build_live_apply_plan(evidence, stage, changes)

    assert verified == [stage]
    assert result.deployment_id == evidence.deployment_id
    assert result.target == evidence.target
    assert result.repository_id == evidence.repository_id
    assert result.baseline_sha == prepared.evidence.baseline_sha
    assert result.candidate_sha == evidence.candidate_sha
    assert result.stage_manifest_sha256 == evidence.stage_manifest_sha256
    assert [entry.path for entry in result.operations] == [
        "automations.yaml",
        "old.yaml",
        "scripts/new.yaml",
    ]
    modified, deleted, added = result.operations
    assert modified.status == "modified"
    assert modified.baseline_object_id == "3" * 40
    assert modified.candidate_object_id == "1" * 40
    assert modified.staged_sha256 == "1" * 64
    assert deleted.status == "deleted"
    assert deleted.candidate_object_id is None
    assert deleted.staged_sha256 is None
    assert added.status == "added"
    assert added.baseline_object_id is None
    assert added.candidate_mode == "100755"
    with pytest.raises(FrozenInstanceError):
        result.candidate_sha = "e" * 40  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.operations[0].status = "deleted"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target", "other/private-repo"),
        ("repository_id", 54321),
        ("candidate_sha", "e" * 40),
    ],
)
def test_candidate_changes_binding_mismatch_fails_before_stage_verification(
    tmp_path,
    monkeypatch,
    field,
    value,
):
    prepared = _prepared()
    stage = _stage(tmp_path, prepared)
    evidence = _evidence(tmp_path, monkeypatch, prepared, stage)
    changes = replace(_changes(prepared), **{field: value})
    called = False

    def verifier(_stage):
        nonlocal called
        called = True

    monkeypatch.setattr("ha_syncapp.live_apply_plan.verify_candidate_stage", verifier)

    with pytest.raises(LiveApplyPlanError, match="binding"):
        build_live_apply_plan(evidence, stage, changes)

    assert called is False


def test_candidate_entry_must_match_exact_staged_metadata(tmp_path, monkeypatch):
    prepared = _prepared()
    stage = _stage(tmp_path, prepared)
    evidence = _evidence(tmp_path, monkeypatch, prepared, stage)
    changes = _changes(prepared)
    bad = replace(
        changes.changes[0],
        candidate_object_id="9" * 40,
    )
    changes = replace(changes, changes=(bad, *changes.changes[1:]))
    monkeypatch.setattr(
        "ha_syncapp.live_apply_plan.verify_candidate_stage",
        lambda _stage: None,
    )

    with pytest.raises(LiveApplyPlanError, match="staged entry"):
        build_live_apply_plan(evidence, stage, changes)


def test_deleted_path_must_be_absent_from_staged_candidate(tmp_path, monkeypatch):
    prepared = _prepared()
    stage = _stage(tmp_path, prepared)
    stage = replace(
        stage,
        entries=(
            *stage.entries,
            CandidateStageEntry(
                path="old.yaml",
                git_mode="100644",
                object_id="4" * 40,
                size=8,
                sha256="4" * 64,
            ),
        ),
    )
    evidence = _evidence(tmp_path, monkeypatch, prepared, stage)
    monkeypatch.setattr(
        "ha_syncapp.live_apply_plan.verify_candidate_stage",
        lambda _stage: None,
    )

    with pytest.raises(LiveApplyPlanError, match="deleted path"):
        build_live_apply_plan(evidence, stage, _changes(prepared))


def test_stage_integrity_failure_is_sanitized(tmp_path, monkeypatch):
    prepared = _prepared()
    stage = _stage(tmp_path, prepared)
    evidence = _evidence(tmp_path, monkeypatch, prepared, stage)

    def verifier(_stage):
        raise RuntimeError("secret candidate bytes / token detail")

    monkeypatch.setattr("ha_syncapp.live_apply_plan.verify_candidate_stage", verifier)

    with pytest.raises(LiveApplyPlanError) as caught:
        build_live_apply_plan(evidence, stage, _changes(prepared))

    assert str(caught.value) == "candidate Stage integrity re-verification failed"
    assert caught.value.__suppress_context__ is True
    assert "secret" not in str(caught.value)


def test_binding_drift_during_stage_verification_fails_closed(tmp_path, monkeypatch):
    prepared = _prepared()
    stage = _stage(tmp_path, prepared)
    evidence = _evidence(tmp_path, monkeypatch, prepared, stage)

    def verifier(value):
        object.__setattr__(value, "commit_sha", "e" * 40)

    monkeypatch.setattr("ha_syncapp.live_apply_plan.verify_candidate_stage", verifier)

    with pytest.raises(LiveApplyPlanError, match="changed during verification"):
        build_live_apply_plan(evidence, stage, _changes(prepared))


def test_wrong_input_types_fail_closed_without_nested_details(tmp_path, monkeypatch):
    prepared = _prepared()
    stage = _stage(tmp_path, prepared)
    evidence = _evidence(tmp_path, monkeypatch, prepared, stage)

    with pytest.raises(LiveApplyPlanError) as caught:
        build_live_apply_plan(evidence, stage, object())  # type: ignore[arg-type]

    assert caught.value.__suppress_context__ is True
    assert "object" not in str(caught.value)
