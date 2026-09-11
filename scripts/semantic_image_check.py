"""Container-only checks: no mocks, real staged bytes, real bundled Core CLI."""

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

_GROUPS = {"all", "valid", "invalid", "warning", "version"}


def require_failure(inputs: tuple, reason: str) -> None:
    try:
        validate_candidate_semantics(*inputs)
    except CandidateSemanticError as exc:
        assert reason in str(exc), str(exc)
        assert "credential-canary" not in str(exc)
    else:
        raise AssertionError("Unsafe candidate was accepted")


def valid_fixture() -> dict[str, bytes]:
    return {
        "configuration.yaml": (
            b"homeassistant:\n  name: Validator fixture\nscene: !include scenes.yaml\n"
        ),
        "scenes.yaml": b"- name: Example\n  entities:\n    light.example: 'on'\n",
    }


def check_valid(parent: Path, valid: dict[str, bytes]) -> None:
    inputs = candidate_inputs(parent, valid)
    try:
        result = validate_candidate_semantics(*inputs)
    except CandidateSemanticError:
        # The only diagnostic input here is the fixed public valid fixture above.
        # Enter through the dedicated validator executable so CI preserves the same
        # mandatory AppArmor child-profile transition as production validation.
        with tempfile.TemporaryDirectory(prefix="syncapp-validator-", dir="/tmp") as diagnostic:
            config = Path(diagnostic) / "config"
            _copy_stage(inputs[2], config)
            subprocess.run(
                [
                    "/opt/syncapp-validator/python3",
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


def run() -> None:
    group = sys.argv[1] if len(sys.argv) == 2 else "all"
    if group not in _GROUPS or len(sys.argv) > 2:
        raise SystemExit("invalid semantic fixture group")

    with tempfile.TemporaryDirectory() as directory:
        parent = Path(directory)
        valid = valid_fixture()

        if group in {"all", "valid"}:
            check_valid(parent, valid)

        if group in {"all", "invalid"}:
            invalid = dict(
                valid,
                **{"configuration.yaml": b"homeassistant:\n  latitude: credential-canary\n"},
            )
            require_failure(candidate_inputs(parent, invalid), "failed Home Assistant")

        if group in {"all", "warning"}:
            # Core reports this integration schema error as a warning:
            # --fail-on-warnings is essential.
            warning = dict(valid, **{"scenes.yaml": b"- name: Example\n  entities: 42\n"})
            require_failure(candidate_inputs(parent, warning), "failed Home Assistant")

        if group in {"all", "version"}:
            require_failure(candidate_inputs(parent, valid, "2026.9.2"), "version")

    print(f"Exact-version Core semantic fixture verified: {group}")


if __name__ == "__main__":
    run()
