"""Real manifest/evidence fixtures shared by unit and actual-image validation tests."""

import hashlib
from pathlib import Path
from uuid import uuid4

from ha_syncapp.candidate_dependencies import analyze_candidate_dependencies
from ha_syncapp.candidate_impact import expand_candidate_impact
from ha_syncapp.candidate_integrity import CandidateIntegrity
from ha_syncapp.candidate_risk import classify_candidate_risk
from ha_syncapp.candidate_stage import CandidateStage, CandidateStageEntry, _manifest_bytes
from ha_syncapp.candidate_validation import validate_candidate_configuration
from ha_syncapp.core_version_evidence import bind_core_version
from ha_syncapp.runtime_inventory import RuntimeInventoryInput


def candidate_inputs(parent: Path, files: dict[str, bytes], version: str = "2026.9.1") -> tuple:
    root = parent / uuid4().hex
    root.mkdir(mode=0o700)
    tree = root / "tree"
    tree.mkdir(mode=0o700)
    entries = []
    for name, data in sorted(files.items()):
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        for directory in path.parents:
            if directory == root:
                break
            directory.chmod(0o700)
        path.write_bytes(data)
        path.chmod(0o600)
        oid = hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False)
        entries.append(
            CandidateStageEntry(
                name, "100644", oid.hexdigest(), len(data), hashlib.sha256(data).hexdigest()
            )
        )
    manifest = _manifest_bytes(
        target="Owner/Home",
        repository_id=42,
        branch="candidate",
        commit_sha="b" * 40,
        entries=tuple(entries),
    )
    (root / "manifest.json").write_bytes(manifest)
    (root / "manifest.json").chmod(0o600)
    stage = CandidateStage(
        root,
        tree,
        root / "manifest.json",
        hashlib.sha256(manifest).hexdigest(),
        "Owner/Home",
        42,
        "candidate",
        "b" * 40,
        tuple(entries),
    )
    integrity = CandidateIntegrity(
        "Owner/Home",
        42,
        "a" * 40,
        "b" * 40,
        stage.manifest_sha256,
        tuple(sorted(files)),
    )
    runtime = RuntimeInventoryInput(manifest={"core_config": {"version": version}})
    dependencies = analyze_candidate_dependencies(integrity, stage, runtime)
    impact = expand_candidate_impact(dependencies, runtime)
    risk = classify_candidate_risk(dependencies, impact, runtime)
    static = validate_candidate_configuration(integrity, stage, dependencies, impact, risk, runtime)
    return static, integrity, stage, dependencies, impact, risk, runtime, bind_core_version(runtime)
