from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_sandbox_probe_diagnostics_are_fixed_and_sanitized() -> None:
    probe = (ROOT / "scripts/sandbox_image_probe.py").read_text()
    required = (
        'stage = "apparmor-profile"',
        'stage = "enter-sandbox"',
        'stage = "deny-homeassistant"',
        'stage = "deny-other-exec"',
        'stage = "allow-validator-reexec"',
        'stage = "allow-workspace-io"',
        'print(f"sandbox probe failed: {stage}", file=sys.stderr, flush=True)',
        "raise SystemExit(1) from None",
    )
    for text in required:
        assert text in probe
    assert "str(exc)" not in probe
    assert "repr(exc)" not in probe


def test_native_ci_names_each_sandbox_security_boundary() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    titles = (
        "Verify semantic validator AppArmor transition",
        "Verify semantic sandbox entry",
        "Verify semantic sandbox identity boundary",
        "Verify semantic sandbox image marker",
        "Verify semantic sandbox filesystem boundary",
        "Verify semantic sandbox network boundary",
        "Verify semantic sandbox executable boundary",
        "Verify semantic sandbox runtime and workspace boundary",
    )
    for title in titles:
        assert title in workflow

    prefix = "python3 scripts/semantic_container_smoke.py semantic_sandbox_image_check.py "
    groups = (
        "profile",
        "sandbox-entry",
        "identity",
        "image-marker",
        "filesystem",
        "network",
        "exec",
        "runtime-workspace",
    )
    for group in groups:
        assert f"{prefix}{group}" in workflow

    assert "continue-on-error" not in workflow
