from __future__ import annotations

import hashlib
import os
from pathlib import Path

import ha_syncapp.candidate_dependencies as dependency_module
import pytest
from ha_syncapp.candidate_integrity import CandidateIntegrity
from ha_syncapp.candidate_stage import CandidateStage, CandidateStageEntry, CandidateStageError
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


def _integrity(*paths: str) -> CandidateIntegrity:
    return CandidateIntegrity(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        stage_manifest_sha256=MANIFEST_SHA,
        changed_paths=tuple(sorted(paths)),
    )


def _runtime() -> RuntimeInventoryInput:
    return RuntimeInventoryInput(
        manifest={},
        homeassistant={
            "entities": [
                {"entity_id": "light.kitchen"},
                {"entity_id": "sensor.temperature"},
            ]
        },
    )


def _install_stage_verifier(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        dependency_module.stage_module,
        "verify_candidate_stage",
        lambda stage: calls.append(stage.commit_sha),
    )
    return calls


def test_analyzes_only_integrity_bound_changed_candidate_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_stage_verifier(monkeypatch)
    stage = _stage(
        tmp_path,
        {
            "automations.yaml": (
                b"entity_id: light.kitchen\n"
                b"other: sensor.not_registered\n"
                b"value_template: \"{{ states('sensor.temperature') }}\"\n"
            ),
            "unchanged.yaml": b"entity_id: sensor.temperature\n",
        },
    )

    result = dependency_module.analyze_candidate_dependencies(
        _integrity("automations.yaml", "scripts/deleted.yaml"),
        stage,
        _runtime(),
    )

    assert calls == [CANDIDATE_SHA, CANDIDATE_SHA]
    assert result.target == TARGET
    assert result.repository_id == REPOSITORY_ID
    assert result.baseline_sha == BASELINE_SHA
    assert result.candidate_sha == CANDIDATE_SHA
    assert result.stage_manifest_sha256 == MANIFEST_SHA
    assert result.files == (
        dependency_module.CandidateDependencyFile(
            path="automations.yaml",
            disposition="analyzed_text",
            known_entity_references=("light.kitchen", "sensor.temperature"),
            unknown_object_references=("sensor.not_registered",),
            dynamic_reference=True,
        ),
        dependency_module.CandidateDependencyFile(
            path="scripts/deleted.yaml",
            disposition="deleted",
            known_entity_references=(),
            unknown_object_references=(),
            dynamic_reference=False,
        ),
    )
    assert result.known_entity_references == ("light.kitchen", "sensor.temperature")
    assert result.unknown_object_references == ("sensor.not_registered",)
    assert result.dynamic_paths == ("automations.yaml",)
    assert result.unanalyzed_paths == ()


def test_output_is_deterministic_and_inputs_are_not_mutated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stage_verifier(monkeypatch)
    files = {
        "z.yaml": b"sensor.temperature light.kitchen\n",
        "a.yaml": b"light.kitchen\n",
    }
    stage = _stage(tmp_path, files)
    integrity = _integrity("z.yaml", "a.yaml")
    runtime = _runtime()
    original_homeassistant = dict(runtime.homeassistant)

    first = dependency_module.analyze_candidate_dependencies(integrity, stage, runtime)
    second = dependency_module.analyze_candidate_dependencies(integrity, stage, runtime)

    assert first == second
    assert tuple(item.path for item in first.files) == ("a.yaml", "z.yaml")
    assert runtime.homeassistant == original_homeassistant


def test_binary_and_oversize_files_are_explicitly_unanalyzed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stage_verifier(monkeypatch)
    monkeypatch.setattr(dependency_module, "_MAX_ANALYSIS_BYTES", 4)
    stage = _stage(
        tmp_path,
        {
            "binary.bin": b"\x00\xff",
            "large.yaml": b"12345",
        },
    )

    result = dependency_module.analyze_candidate_dependencies(
        _integrity("binary.bin", "large.yaml"),
        stage,
        _runtime(),
    )

    assert tuple((item.path, item.disposition) for item in result.files) == (
        ("binary.bin", "non_utf8_or_binary"),
        ("large.yaml", "oversize"),
    )
    assert result.unanalyzed_paths == ("binary.bin", "large.yaml")


def test_binding_mismatch_fails_before_stage_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_stage_verifier(monkeypatch)
    stage = _stage(tmp_path, {"configuration.yaml": b"light.kitchen\n"})
    integrity = CandidateIntegrity(
        target=TARGET,
        repository_id=REPOSITORY_ID + 1,
        baseline_sha=BASELINE_SHA,
        candidate_sha=CANDIDATE_SHA,
        stage_manifest_sha256=MANIFEST_SHA,
        changed_paths=("configuration.yaml",),
    )

    with pytest.raises(dependency_module.CandidateDependencyError, match="bindings"):
        dependency_module.analyze_candidate_dependencies(integrity, stage, _runtime())

    assert calls == []


def test_changed_candidate_file_digest_must_still_match_stage_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stage_verifier(monkeypatch)
    stage = _stage(tmp_path, {"configuration.yaml": b"light.kitchen\n"})
    (stage.tree / "configuration.yaml").write_bytes(b"sensor.temperature\n")
    os.chmod(stage.tree / "configuration.yaml", 0o600)

    with pytest.raises(dependency_module.CandidateDependencyError, match="staged bytes"):
        dependency_module.analyze_candidate_dependencies(
            _integrity("configuration.yaml"), stage, _runtime()
        )


def test_stage_reverification_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage(tmp_path, {"configuration.yaml": b"light.kitchen\n"})

    def fail_verification(candidate_stage: CandidateStage) -> None:
        raise CandidateStageError(f"secret bytes in {candidate_stage.tree}")

    monkeypatch.setattr(
        dependency_module.stage_module,
        "verify_candidate_stage",
        fail_verification,
    )

    with pytest.raises(dependency_module.CandidateDependencyError) as exc_info:
        dependency_module.analyze_candidate_dependencies(
            _integrity("configuration.yaml"), stage, _runtime()
        )

    assert str(exc_info.value) == "candidate dependency evidence could not be established"
    assert "secret bytes" not in str(exc_info.value)
