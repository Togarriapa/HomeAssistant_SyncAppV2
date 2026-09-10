from dataclasses import replace
from pathlib import Path

import ha_syncapp.candidate_semantics as semantic
import pytest
from semantic_fixtures import candidate_inputs


@pytest.fixture(autouse=True)
def unit_copy_ownership(monkeypatch):
    # Identity transitions are exercised by the actual-image sandbox tests. The local
    # single-UID development filesystem cannot represent another Unix owner.
    monkeypatch.setattr(semantic.os, "chown", lambda *_args: None)


def test_success_binds_every_gate_and_uses_a_copy(tmp_path, monkeypatch):
    inputs = candidate_inputs(tmp_path, {"configuration.yaml": b"homeassistant:\n"})
    stage = inputs[2]
    copied = []

    def run(config, version):
        assert config != stage.tree
        assert config.joinpath("configuration.yaml").read_bytes() == b"homeassistant:\n"
        assert version == "2026.9.1"
        copied.append(config)

    monkeypatch.setattr(semantic, "_run_validator", run)
    result = semantic.validate_candidate_semantics(*inputs)
    assert result.repository_id == 42
    assert result.candidate_sha == stage.commit_sha
    assert result.stage_manifest_sha256 == stage.manifest_sha256
    assert result.runtime_sha256 == inputs[-1].runtime_sha256
    assert result.core_version == "2026.9.1"
    assert result.risk_level == inputs[5].level
    assert result.validator == "homeassistant.check_config.fail_on_warnings"
    assert not copied[0].exists()
    semantic.verify_candidate_semantic_validation(result, *inputs)
    with pytest.raises(semantic.CandidateSemanticError):
        semantic.verify_candidate_semantic_validation(replace(result, candidate_sha="c" * 40), *inputs)


@pytest.mark.parametrize("version", ["2026.8.1", "2026.9.0", "2026.9.2"])
def test_exact_version_mismatch_never_launches(tmp_path, monkeypatch, version):
    inputs = candidate_inputs(tmp_path, {"configuration.yaml": b"homeassistant:\n"}, version)
    monkeypatch.setattr(semantic, "_run_validator", lambda *args: pytest.fail("must not launch"))
    with pytest.raises(semantic.CandidateSemanticError, match="version"):
        semantic.validate_candidate_semantics(*inputs)


@pytest.mark.parametrize("files", [
    {"configuration.yaml": b"broken: ["},
    {"configuration.yaml": b"homeassistant:\n", "notes.txt": b"not validated"},
])
def test_invalid_or_unvalidated_static_gate_never_launches(tmp_path, monkeypatch, files):
    inputs = candidate_inputs(tmp_path, files)
    monkeypatch.setattr(semantic, "_run_validator", lambda *args: pytest.fail("must not launch"))
    with pytest.raises(semantic.CandidateSemanticError, match="static"):
        semantic.validate_candidate_semantics(*inputs)


@pytest.mark.parametrize("name, data", [
    ("configuration.yaml", b"homeassistant: !include /etc/passwd\n"),
    ("configuration.yaml", b"homeassistant: !include ../secrets.yaml\n"),
    ("configuration.yaml", b"homeassistant: !include_dir_merge_named /data\n"),
    ("unused.yaml", b"key: !include /homeassistant/secrets.yaml\n"),
    ("custom_components/example/manifest.json", b"{}"),
    ("deps/example.json", b"{}"),
])
def test_unsupported_tree_fails_before_launch(tmp_path, monkeypatch, name, data):
    files = {"configuration.yaml": b"homeassistant:\n", name: data}
    inputs = candidate_inputs(tmp_path, files)
    monkeypatch.setattr(semantic, "_run_validator", lambda *args: pytest.fail("must not launch"))
    with pytest.raises(semantic.CandidateSemanticError, match="unsupported"):
        semantic.validate_candidate_semantics(*inputs)


@pytest.mark.parametrize("mutate_copy", [True, False])
def test_mutation_during_check_discards_success_and_cleans_copy(tmp_path, monkeypatch, mutate_copy):
    inputs = candidate_inputs(tmp_path, {"configuration.yaml": b"homeassistant:\n"})
    copies = []

    def run(config, _version):
        copies.append(config)
        root = config if mutate_copy else inputs[2].tree
        (root / "configuration.yaml").write_bytes(b"secret: changed\n")

    monkeypatch.setattr(semantic, "_run_validator", run)
    with pytest.raises(semantic.CandidateSemanticError):
        semantic.validate_candidate_semantics(*inputs)
    assert not copies[0].exists()


def test_validator_error_does_not_leak_candidate_bytes(tmp_path, monkeypatch):
    inputs = candidate_inputs(tmp_path, {"configuration.yaml": b"homeassistant:\n"})
    copies = []

    def run(config: Path, _version: str):
        copies.append(config)
        raise OSError("credential-canary-not-for-logs")

    monkeypatch.setattr(semantic, "_run_validator", run)
    with pytest.raises(semantic.CandidateSemanticError) as caught:
        semantic.validate_candidate_semantics(*inputs)
    assert "credential-canary" not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert not copies[0].exists()


def test_runtime_drift_during_check_discards_success(tmp_path, monkeypatch):
    inputs = candidate_inputs(tmp_path, {"configuration.yaml": b"homeassistant:\n"})

    def run(*_args):
        inputs[6].manifest["core_config"]["version"] = "2026.9.2"

    monkeypatch.setattr(semantic, "_run_validator", run)
    with pytest.raises(semantic.CandidateSemanticError):
        semantic.validate_candidate_semantics(*inputs)
