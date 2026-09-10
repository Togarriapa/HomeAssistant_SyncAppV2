from pathlib import Path

import ha_syncapp.candidate_integrity as integrity_module
import pytest
from ha_syncapp.candidate_changes import CandidateChange, CandidateChanges
from ha_syncapp.candidate_fetch import CandidateFetch
from ha_syncapp.candidate_integrity import CandidateIntegrityError, validate_candidate_integrity
from ha_syncapp.candidate_stage import CandidateStage, CandidateStageEntry, CandidateStageError

TARGET = "Owner/Home"
REPOSITORY_ID = 42
BASELINE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
OLD_ID = "c" * 40
NEW_ID = "d" * 40


def _fetched(tmp_path: Path) -> CandidateFetch:
    return CandidateFetch(
        root=tmp_path / "fetch",
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="candidate",
        commit_sha=CANDIDATE_SHA,
        git_ref="refs/syncapp/candidate-fetch",
    )


def _stage(tmp_path: Path) -> CandidateStage:
    root = tmp_path / "stage"
    return CandidateStage(
        root=root,
        tree=root / "tree",
        manifest=root / "manifest.json",
        manifest_sha256="e" * 64,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="candidate",
        commit_sha=CANDIDATE_SHA,
        entries=(
            CandidateStageEntry(
                path="configuration.yaml",
                git_mode="100644",
                object_id=NEW_ID,
                size=1,
                sha256="f" * 64,
            ),
            CandidateStageEntry(
                path="scripts/new.sh",
                git_mode="100755",
                object_id=NEW_ID,
                size=1,
                sha256="1" * 64,
            ),
        ),
    )


def _changes(*items: CandidateChange) -> CandidateChanges:
    return CandidateChanges(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        changes=tuple(items),
    )


def _install_verification_fakes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        integrity_module.stage_module,
        "verify_candidate_stage",
        lambda stage: calls.append("stage"),
    )
    monkeypatch.setattr(
        integrity_module.stage_module,
        "_reprove_fetch",
        lambda fetched: calls.append("fetch"),
    )
    return calls


def test_valid_integrity_evidence_is_identity_bound_and_reverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_verification_fakes(monkeypatch)
    changes = _changes(
        CandidateChange(
            path="configuration.yaml",
            status="modified",
            baseline_mode="100644",
            baseline_object_id=OLD_ID,
            candidate_mode="100644",
            candidate_object_id=NEW_ID,
        ),
        CandidateChange(
            path="scripts/new.sh",
            status="added",
            baseline_mode=None,
            baseline_object_id=None,
            candidate_mode="100755",
            candidate_object_id=NEW_ID,
        ),
    )

    result = validate_candidate_integrity(_fetched(tmp_path), _stage(tmp_path), changes)

    assert result.target == TARGET
    assert result.repository_id == REPOSITORY_ID
    assert result.baseline_sha == BASELINE_SHA
    assert result.candidate_sha == CANDIDATE_SHA
    assert result.stage_manifest_sha256 == "e" * 64
    assert result.changed_paths == ("configuration.yaml", "scripts/new.sh")
    assert calls == ["stage", "fetch", "stage", "fetch"]


def test_no_change_candidate_can_pass_integrity_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verification_fakes(monkeypatch)

    result = validate_candidate_integrity(_fetched(tmp_path), _stage(tmp_path), _changes())

    assert result.changed_paths == ()


def test_binding_mismatch_fails_before_verification(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    changes = CandidateChanges(
        target=TARGET,
        repository_id=REPOSITORY_ID + 1,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        changes=(),
    )

    with pytest.raises(CandidateIntegrityError, match="bindings"):
        validate_candidate_integrity(_fetched(tmp_path), stage, changes)


@pytest.mark.parametrize(
    "change",
    [
        CandidateChange("configuration.yaml", "unknown", "100644", OLD_ID, "100644", NEW_ID),
        CandidateChange("configuration.yaml", "added", "100644", OLD_ID, "100644", NEW_ID),
        CandidateChange("configuration.yaml", "deleted", None, None, "100644", NEW_ID),
        CandidateChange("configuration.yaml", "modified", "100644", OLD_ID, "100755", NEW_ID),
        CandidateChange("configuration.yaml", "mode_changed", "100644", OLD_ID, "100755", NEW_ID),
        CandidateChange("configuration.yaml", "modified", "100644", OLD_ID, "100644", OLD_ID),
    ],
)
def test_invalid_status_shapes_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: CandidateChange,
) -> None:
    _install_verification_fakes(monkeypatch)

    with pytest.raises(CandidateIntegrityError):
        validate_candidate_integrity(_fetched(tmp_path), _stage(tmp_path), _changes(change))


def test_candidate_side_change_must_match_stage_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verification_fakes(monkeypatch)
    change = CandidateChange(
        path="configuration.yaml",
        status="modified",
        baseline_mode="100644",
        baseline_object_id=OLD_ID,
        candidate_mode="100755",
        candidate_object_id=NEW_ID,
    )

    with pytest.raises(CandidateIntegrityError):
        validate_candidate_integrity(_fetched(tmp_path), _stage(tmp_path), _changes(change))


def test_duplicate_or_out_of_order_paths_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verification_fakes(monkeypatch)
    item = CandidateChange(
        path="configuration.yaml",
        status="modified",
        baseline_mode="100644",
        baseline_object_id=OLD_ID,
        candidate_mode="100644",
        candidate_object_id=NEW_ID,
    )

    with pytest.raises(CandidateIntegrityError, match="canonically ordered"):
        validate_candidate_integrity(_fetched(tmp_path), _stage(tmp_path), _changes(item, item))


def test_unsafe_path_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verification_fakes(monkeypatch)
    item = CandidateChange(
        path=".git/config",
        status="deleted",
        baseline_mode="100644",
        baseline_object_id=OLD_ID,
        candidate_mode=None,
        candidate_object_id=None,
    )

    with pytest.raises(CandidateIntegrityError, match="unsafe"):
        validate_candidate_integrity(_fetched(tmp_path), _stage(tmp_path), _changes(item))


def test_stage_tamper_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(stage: CandidateStage) -> None:
        raise CandidateStageError("sensitive staging detail")

    monkeypatch.setattr(integrity_module.stage_module, "verify_candidate_stage", fail)

    with pytest.raises(CandidateIntegrityError) as caught:
        validate_candidate_integrity(_fetched(tmp_path), _stage(tmp_path), _changes())

    assert "sensitive staging detail" not in str(caught.value)


def test_second_reverification_failure_prevents_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def verify(stage: CandidateStage) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CandidateStageError("changed")

    monkeypatch.setattr(integrity_module.stage_module, "verify_candidate_stage", verify)
    monkeypatch.setattr(integrity_module.stage_module, "_reprove_fetch", lambda fetched: None)

    with pytest.raises(CandidateIntegrityError, match="changed during"):
        validate_candidate_integrity(_fetched(tmp_path), _stage(tmp_path), _changes())
