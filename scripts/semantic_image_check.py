"""Container-only checks: no mocks, real staged bytes, real bundled Core CLI."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "/fixtures")

from ha_syncapp.candidate_semantics import (  # noqa: E402
    CandidateSemanticError,
    _copy_stage,
    validate_candidate_semantics,
    verify_candidate_semantic_validation,
)
from semantic_fixtures import candidate_inputs  # noqa: E402


def require_failure(inputs: tuple, reason: str) -> None:
    try:
        validate_candidate_semantics(*inputs)
    except CandidateSemanticError as exc:
        assert reason in str(exc), str(exc)
        assert "credential-canary" not in str(exc)
    else:
        raise AssertionError("Unsafe candidate was accepted")


def verify_sandbox() -> None:
    # Readable canary outside the isolated config must remain inaccessible to the child.
    canary = Path("/tmp/world-readable-canary")
    canary.write_text("credential-canary")
    canary.chmod(0o644)
    with tempfile.TemporaryDirectory(prefix="syncapp-validator-") as directory:
        probe_root = Path(directory)
        config = probe_root / "config"
        config.mkdir(mode=0o700)
        os.chown(config, 65534, 65534)
        os.chown(probe_root, 65534, 65534)
        output = subprocess.check_output(
            [
                "/opt/syncapp-validator/python3",
                "-I",
                "-B",
                "/checks/sandbox_image_probe.py",
                str(config),
            ],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            text=True,
            timeout=30,
        )
        assert output.strip() == "sandbox verified", output


def run() -> None:
    verify_sandbox()
    with tempfile.TemporaryDirectory() as directory:
        parent = Path(directory)
        valid = {
            "configuration.yaml": (
                b"homeassistant:\n  name: Validator fixture\nscene: !include scenes.yaml\n"
            ),
            "scenes.yaml": b"- name: Example\n  entities:\n    light.example: 'on'\n",
        }
        inputs = candidate_inputs(parent, valid)
        try:
            result = validate_candidate_semantics(*inputs)
        except CandidateSemanticError:
            # The only diagnostic input here is the fixed public valid fixture above.
            # Production continues to suppress all raw Core output.
            with tempfile.TemporaryDirectory(prefix="fixture-diagnostic-") as diagnostic:
                config = Path(diagnostic) / "config"
                _copy_stage(inputs[2], config)
                subprocess.run(
                    [
                        "/usr/local/bin/python3",
                        "-I",
                        "-B",
                        "/checks/core_fixture_probe.py",
                        str(config),
                    ],
                    env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                    check=False,
                    timeout=30,
                )
            raise
        verify_candidate_semantic_validation(result, *inputs)

        # Syntactically valid YAML must still fail real Core schema validation.
        invalid = dict(
            valid, **{"configuration.yaml": b"homeassistant:\n  latitude: credential-canary\n"}
        )
        require_failure(candidate_inputs(parent, invalid), "failed Home Assistant")
        # Core reports this integration schema error as a warning: --fail-on-warnings is essential.
        warning = dict(valid, **{"scenes.yaml": b"- name: Example\n  entities: 42\n"})
        require_failure(candidate_inputs(parent, warning), "failed Home Assistant")
        require_failure(candidate_inputs(parent, valid, "2026.9.2"), "version")
    print("Exact-version Core valid/invalid/warning checks and AppArmor isolation verified")


if __name__ == "__main__":
    run()
