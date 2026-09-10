"""Version-matched Core check_config evidence for a verified, isolated candidate."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import stat
import subprocess  # nosec B404
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .candidate_dependencies import CandidateDependencyAnalysis
from .candidate_impact import CandidateImpactAnalysis
from .candidate_integrity import CandidateIntegrity
from .candidate_risk import CandidateRiskClassification
from .candidate_stage import CandidateStage, verify_candidate_stage
from .candidate_validation import (
    CandidateStaticValidation,
    _read_bound_bytes,
    verify_candidate_static_validation,
)
from .core_version_evidence import CoreVersionEvidence, verify_core_version_evidence
from .runtime_inventory import RuntimeInventoryInput

# Updated together with the pinned official Core image and actual-image CI fixtures.
BUNDLED_CORE_VERSION = "2026.9.1"
_VALIDATOR = "homeassistant.check_config.fail_on_warnings"
_CORE_PYTHON = "/usr/local/bin/python3"
_TIMEOUT_SECONDS = 180
_MAX_STAGE_BYTES = 128 * 1024 * 1024
_MAX_YAML_BYTES = 4 * 1024 * 1024


class CandidateSemanticError(RuntimeError):
    """No semantic authorization exists; only fixed, secret-free reasons are exposed."""


@dataclass(frozen=True, slots=True)
class CandidateSemanticValidation:
    """Successful check bound to all upstream evidence; never sufficient to Apply alone."""

    target: str
    repository_id: int
    baseline_sha: str
    candidate_sha: str
    stage_manifest_sha256: str
    runtime_sha256: str
    risk_level: str
    core_version: str
    validator: str = _VALIDATOR


def validate_candidate_semantics(
    static: CandidateStaticValidation,
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    risk: CandidateRiskClassification,
    runtime: RuntimeInventoryInput,
    version: CoreVersionEvidence,
) -> CandidateSemanticValidation:
    """Check exact Stage bytes with the bundled Core CLI; no live API calls or writes."""
    try:
        result = _verify_inputs(
            static, integrity, stage, dependencies, impact, risk, runtime, version
        )
        _check_supported_tree(stage)
        # Explicit /tmp avoids an inherited TMPDIR pointing into the live config or app state.
        with tempfile.TemporaryDirectory(
            prefix="syncapp-validator-",
            dir="/tmp",  # nosec B108
        ) as directory:
            config = Path(directory) / "config"
            _copy_stage(stage, config)
            _run_validator(config, version.version)
            _verify_copy(stage, config)
        if result != _verify_inputs(
            static, integrity, stage, dependencies, impact, risk, runtime, version
        ):
            raise CandidateSemanticError("semantic input evidence changed")
        return result
    except CandidateSemanticError:
        raise
    except Exception:
        # Upstream parser/OS exceptions can contain raw candidate values. Never chain them.
        raise CandidateSemanticError(
            "semantic validation evidence could not be established"
        ) from None


def verify_candidate_semantic_validation(
    result: CandidateSemanticValidation,
    static: CandidateStaticValidation,
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    risk: CandidateRiskClassification,
    runtime: RuntimeInventoryInput,
    version: CoreVersionEvidence,
) -> None:
    """Reprove bindings and unchanged bytes before consuming trusted in-process evidence."""
    try:
        expected = _verify_inputs(
            static, integrity, stage, dependencies, impact, risk, runtime, version
        )
        if type(result) is not CandidateSemanticValidation or result != expected:
            raise CandidateSemanticError("semantic validation evidence does not match inputs")
    except CandidateSemanticError:
        raise
    except Exception:
        raise CandidateSemanticError("semantic validation evidence could not be verified") from None


def _verify_inputs(
    static: CandidateStaticValidation,
    integrity: CandidateIntegrity,
    stage: CandidateStage,
    dependencies: CandidateDependencyAnalysis,
    impact: CandidateImpactAnalysis,
    risk: CandidateRiskClassification,
    runtime: RuntimeInventoryInput,
    version: CoreVersionEvidence,
) -> CandidateSemanticValidation:
    verify_candidate_static_validation(
        static, integrity, stage, dependencies, impact, risk, runtime
    )
    if not static.syntax_valid or static.invalid_paths or static.unvalidated_paths:
        raise CandidateSemanticError("successful complete static validation is required")
    verify_core_version_evidence(version, runtime)
    if version.version != BUNDLED_CORE_VERSION:
        raise CandidateSemanticError(
            "running Core version does not match bundled validator version"
        )
    return CandidateSemanticValidation(
        static.target,
        static.repository_id,
        static.baseline_sha,
        static.candidate_sha,
        static.stage_manifest_sha256,
        static.runtime_sha256,
        static.risk_level,
        version.version,
    )


def _check_supported_tree(stage: CandidateStage) -> None:
    if len(stage.entries) > 10_000 or sum(entry.size for entry in stage.entries) > _MAX_STAGE_BYTES:
        raise CandidateSemanticError("unsupported semantic candidate size")
    if "configuration.yaml" not in {entry.path for entry in stage.entries}:
        raise CandidateSemanticError("unsupported candidate without configuration.yaml")
    for entry in stage.entries:
        path = PurePosixPath(entry.path)
        if path.parts[0].casefold() in {"custom_components", "deps"}:
            raise CandidateSemanticError("unsupported candidate-provided integration code")
        if path.suffix.casefold() not in {".yaml", ".yml"}:
            continue
        if entry.size > _MAX_YAML_BYTES:
            raise CandidateSemanticError("unsupported semantic YAML size")
        try:
            documents = list(
                yaml.compose_all(_read_bound_bytes(stage, entry), Loader=yaml.SafeLoader)
            )
            for document in documents:
                if document is not None:
                    _check_includes(document, path)
        except (yaml.YAMLError, RecursionError, ValueError):
            raise CandidateSemanticError("unsupported semantic YAML structure") from None


def _check_includes(root: Node, path: PurePosixPath) -> None:
    pending = [root]
    visited: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in visited:
            continue
        visited.add(id(node))
        if len(visited) > 100_000:
            raise CandidateSemanticError("unsupported semantic YAML complexity")
        if node.tag.startswith("!include"):
            if not isinstance(node, ScalarNode):
                raise CandidateSemanticError("unsupported semantic include")
            value = node.value
            target = PurePosixPath(value)
            if (
                not value
                or target.is_absolute()
                or ".." in target.parts
                or "\\" in value
                or any(ord(char) < 32 for char in value)
                or (path.parent / target).is_absolute()
            ):
                raise CandidateSemanticError("unsupported semantic include outside candidate")
        if isinstance(node, MappingNode):
            pending.extend(child for pair in node.value for child in pair)
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)


def _copy_stage(stage: CandidateStage, config: Path) -> None:
    config.mkdir(mode=0o700)
    for entry in stage.entries:
        path = config.joinpath(*entry.path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_read_bound_bytes(stage, entry))
        path.chmod(0o600)
    verify_candidate_stage(stage)
    # Core may create disposable registry bookkeeping. Original Stage permissions stay unchanged.
    for path in [*config.rglob("*"), config, config.parent]:
        if path.is_dir():
            path.chmod(0o700)
        if os.geteuid() == 0:
            os.chown(path, 65534, 65534)


def _verify_copy(stage: CandidateStage, config: Path) -> None:
    for entry in stage.entries:
        path = config.joinpath(*entry.path.split("/"))
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != entry.size:
            raise CandidateSemanticError("semantic validator changed candidate input files")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry.sha256:
            raise CandidateSemanticError("semantic validator changed candidate input bytes")


def _run_validator(config: Path, version: str) -> None:
    nonce = secrets.token_hex(32)
    command = [
        _CORE_PYTHON,
        "-I",
        "-B",
        str(Path(__file__).with_name("_validator_child.py")),
        str(config),
        version,
        nonce,
    ]
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TMPDIR": str(config.parent),
        "XDG_CACHE_HOME": str(config.parent),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }
    # The helper suppresses Core output at the descriptor level and writes one tiny receipt.
    # A file, not a pipe, also bounds memory if a broken helper emits excessive output.
    with tempfile.TemporaryFile() as receipt:
        with subprocess.Popen(  # nosec B603
            command,
            stdin=subprocess.DEVNULL,
            stdout=receipt,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env=environment,
            close_fds=True,
            start_new_session=True,
        ) as process:
            try:
                returncode = process.wait(timeout=_TIMEOUT_SECONDS)
            except BaseException:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise CandidateSemanticError(
                    "semantic validator timed out or was interrupted"
                ) from None
        receipt.seek(0)
        raw = receipt.read(4097)
    if returncode != 0 or len(raw) > 4096:
        raise CandidateSemanticError("semantic validator unavailable or resource limit exceeded")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CandidateSemanticError("semantic validator returned an ambiguous result") from None
    if payload != {"nonce": nonce, "version": version, "result": "passed"}:
        # No child-generated text (even unknown result fields) is exposed to the app logger.
        if isinstance(payload, dict) and payload.get("result") == "invalid":
            raise CandidateSemanticError("candidate failed Home Assistant semantic validation")
        raise CandidateSemanticError("semantic validator could not establish an isolated result")
