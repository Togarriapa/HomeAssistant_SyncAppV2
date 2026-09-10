from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import ha_syncapp.candidate_validation as validation_module
import pytest
from ha_syncapp.candidate_dependencies import (
    CandidateDependencyAnalysis,
    CandidateDependencyFile,
)
from ha_syncapp.candidate_impact import CandidateImpactAnalysis
from ha_syncapp.candidate_integrity import CandidateIntegrity
from ha_syncapp.candidate_risk import CandidateRiskClassification
from ha_syncapp.candidate_stage import CandidateStage, CandidateStageEntry
from ha_syncapp.runtime_evidence import fingerprint_runtime
from ha_syncapp.runtime_inventory import RuntimeInventoryInput

TARGET = "Owner/Home"
REPOSITORY_ID = 42
BASELINE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
MANIFEST_SHA = "c" * 64


def _entry(path: str, data: bytes) -> CandidateStageEntry:
    return CandidateStageEntry(
        path=path,
        git_mode="100644",
        object_id="d" * 40,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _stage(tmp_path: Path, files: dict[str, bytes]) -> CandidateStage:
    root = tmp_path / "stage"
    tree = root / "tree"
    tree.mkdir(parents=True)
    entries: list[CandidateStageEntry] = []
    for path, data in sorted(files.items()):
        destination = tree.joinpath(*path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        os.chmod(destination, 0o600)
        entries.append(_entry(path, data))
    return CandidateStage(
        root=root,
        tree=tree,
        manifest=root / "manifest.json",
        manifest_sha256=MANIFEST_SHA,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="candidate",
        commit_sha=CANDIDATE_SHA,
        entries=tuple(entries),
    )


def _evidence(paths: tuple[str, ...]) -> tuple[
    CandidateIntegrity,
    CandidateDependencyAnalysis,
    CandidateImpactAnalysis,
    CandidateRiskClassification,
    RuntimeInventoryInput,
]:
    runtime = RuntimeInventoryInput(manifest={}, homeassistant={})
    runtime_sha = fingerprint_runtime(runtime)
    integrity = CandidateIntegrity(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        stage_manifest_sha256=MANIFEST_SHA,
        changed_paths=paths,
    )
    files = tuple(
        CandidateDependencyFile(
            path=path,
            disposition="analyzed_text",
            known_entity_references=(),
            unknown_object_references=(),
            dynamic_reference=False,
        )
        for path in paths
    )
    dependencies = CandidateDependencyAnalysis(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        stage_manifest_sha256=MANIFEST_SHA,
        runtime_sha256=runtime_sha,
        analysis_method="best_effort_lexical",
        files=files,
        known_entity_references=(),
        unknown_object_references=(),
        dynamic_paths=(),
        unanalyzed_paths=(),
    )
    impact = CandidateImpactAnalysis(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        stage_manifest_sha256=MANIFEST_SHA,
        runtime_sha256=runtime_sha,
        entities=(),
    )
    risk = CandidateRiskClassification(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        stage_manifest_sha256=MANIFEST_SHA,
        runtime_sha256=runtime_sha,
        level="low",
        reasons=("low path automations.yaml: automation, script, or scene configuration",),
        changed_paths=paths,
        affected_entities=(),
        unresolved_references=(),
        dynamic_paths=(),
        unanalyzed_paths=(),
    )
    return integrity, dependencies, impact, risk, runtime


def _validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, bytes],
    *paths: str,
) -> validation_module.CandidateStaticValidation:
    stage = _stage(tmp_path, files)
    integrity, dependencies, impact, risk, runtime = _evidence(tuple(sorted(paths)))
    calls: list[str] = []
    monkeypatch.setattr(
        validation_module.stage_module,
        "verify_candidate_stage",
        lambda value: calls.append(value.commit_sha),
    )
    monkeypatch.setattr(
        validation_module,
        "verify_candidate_risk_classification",
        lambda *args: None,
    )
    result = validation_module.validate_candidate_configuration(
        integrity,
        stage,
        dependencies,
        impact,
        risk,
        runtime,
    )
    assert calls == [CANDIDATE_SHA, CANDIDATE_SHA]
    return result


def test_valid_yaml_supports_home_assistant_scalar_indirection_tags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _validate(
        tmp_path,
        monkeypatch,
        {
            "configuration.yaml": (
                b"automation: !include automations.yaml\n"
                b"password: !secret example_password\n"
            )
        },
        "configuration.yaml",
    )

    assert result.syntax_valid is True
    assert result.invalid_paths == ()
    assert result.unvalidated_paths == ()
    assert result.files[0].status == "valid"
    assert result.semantic_home_assistant_validation_required is True


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"broken: [yaml\n", "yaml_syntax_or_tag"),
        (b"same: 1\nsame: 2\n", "yaml_syntax_or_tag"),
        (b"value: !python/object:builtins.str unsafe\n", "yaml_syntax_or_tag"),
        (b"a: 1\n---\nb: 2\n", "yaml_multiple_documents"),
    ],
)
def test_invalid_or_ambiguous_yaml_fails_static_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    reason: str,
) -> None:
    result = _validate(
        tmp_path,
        monkeypatch,
        {"configuration.yaml": content},
        "configuration.yaml",
    )

    assert result.syntax_valid is False
    assert result.invalid_paths == ("configuration.yaml",)
    assert result.files[0].reasons == (reason,)


@pytest.mark.parametrize(
    "content",
    [
        b'{"version":1,"data":{"ok":true}',
        b'{"version":1,"version":2}',
        b'{"value":NaN}',
    ],
)
def test_storage_json_rejects_syntax_duplicates_and_nonfinite_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    result = _validate(
        tmp_path,
        monkeypatch,
        {".storage/example": content},
        ".storage/example",
    )

    assert result.syntax_valid is False
    assert result.invalid_paths == (".storage/example",)
    assert result.files[0].format == "json"
    assert result.files[0].reasons == ("json_syntax_or_structure",)


def test_conflict_markers_are_invalid_before_format_specific_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _validate(
        tmp_path,
        monkeypatch,
        {"automations.yaml": b"<<<<<<< ours\na: 1\n=======\na: 2\n>>>>>>> theirs\n"},
        "automations.yaml",
    )

    assert result.syntax_valid is False
    assert result.files[0].reasons == ("merge_conflict_marker",)


def test_binary_and_unsupported_text_remain_explicitly_unvalidated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _validate(
        tmp_path,
        monkeypatch,
        {
            "custom_components/example/blob.bin": b"\x00\xff",
            "notes.txt": b"plain text\n",
        },
        "custom_components/example/blob.bin",
        "notes.txt",
    )

    assert result.syntax_valid is True
    assert result.invalid_paths == ()
    assert result.unvalidated_paths == (
        "custom_components/example/blob.bin",
        "notes.txt",
    )
    assert tuple(item.status for item in result.files) == ("unvalidated", "unvalidated")


def test_deleted_path_is_explicit_and_does_not_require_stage_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _validate(tmp_path, monkeypatch, {}, "scripts/deleted.yaml")

    assert result.files == (
        validation_module.CandidateValidationFile(
            path="scripts/deleted.yaml",
            status="deleted",
            format="deleted",
        ),
    )


def test_mismatched_candidate_binding_fails_before_stage_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage(tmp_path, {"automations.yaml": b"- alias: safe\n"})
    integrity, dependencies, impact, risk, runtime = _evidence(("automations.yaml",))
    bad_stage = replace(stage, repository_id=REPOSITORY_ID + 1)
    monkeypatch.setattr(
        validation_module,
        "verify_candidate_risk_classification",
        lambda *args: None,
    )

    with pytest.raises(validation_module.CandidateValidationError, match="Stage bindings"):
        validation_module.validate_candidate_configuration(
            integrity,
            bad_stage,
            dependencies,
            impact,
            risk,
            runtime,
        )


def test_result_verifier_rejects_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage(tmp_path, {"automations.yaml": b"- alias: safe\n"})
    integrity, dependencies, impact, risk, runtime = _evidence(("automations.yaml",))
    monkeypatch.setattr(validation_module.stage_module, "verify_candidate_stage", lambda value: None)
    monkeypatch.setattr(
        validation_module,
        "verify_candidate_risk_classification",
        lambda *args: None,
    )
    result = validation_module.validate_candidate_configuration(
        integrity, stage, dependencies, impact, risk, runtime
    )
    tampered = replace(result, risk_level="critical")

    with pytest.raises(validation_module.CandidateValidationError, match="does not match"):
        validation_module.verify_candidate_static_validation(
            tampered,
            integrity,
            stage,
            dependencies,
            impact,
            risk,
            runtime,
        )


def test_static_validation_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage(tmp_path, {"automations.yaml": b"- alias: safe\n"})
    integrity, dependencies, impact, risk, runtime = _evidence(("automations.yaml",))
    monkeypatch.setattr(validation_module.stage_module, "verify_candidate_stage", lambda value: None)
    monkeypatch.setattr(
        validation_module,
        "verify_candidate_risk_classification",
        lambda *args: None,
    )

    first = validation_module.validate_candidate_configuration(
        integrity, stage, dependencies, impact, risk, runtime
    )
    second = validation_module.validate_candidate_configuration(
        integrity, stage, dependencies, impact, risk, runtime
    )

    assert first == second
