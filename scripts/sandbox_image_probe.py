"""Exercise the production sandbox in a disposable child, never the test runner."""

import asyncio
import ctypes
import os
import runpy
import socket
import subprocess
import sys
from pathlib import Path

_GROUPS = {"all", "identity", "filesystem", "network", "exec", "runtime-workspace"}


def run() -> None:
    group = sys.argv[2] if len(sys.argv) == 3 else "all"
    if group not in _GROUPS:
        raise SystemExit("invalid sandbox probe group")

    stage = "load-helper"
    try:
        helper = runpy.run_path("/app/ha_syncapp/_validator_child.py", run_name="sandbox_probe")
        config = Path(sys.argv[1])

        stage = "enter-sandbox"
        helper["_sandbox"](config)

        if group in {"all", "identity"}:
            stage = "non-root"
            assert os.geteuid() != 0 and os.getuid() != 0

            stage = "no-new-privileges"
            assert ctypes.CDLL(None).prctl(39, 0, 0, 0, 0) == 1  # PR_GET_NO_NEW_PRIVS

            stage = "apparmor-label"
            assert Path("/proc/self/attr/current").read_bytes().strip() == (
                b"ci_homeassistant_syncapp_v2//validator (enforce)"
            )

            # Core requires this fixed identity marker for the bundled official image.
            stage = "official-image"
            assert Path("/OFFICIAL_IMAGE").is_file()

        if group in {"all", "filesystem"}:
            stage = "deny-tmp-read"
            denied(lambda: Path("/tmp/world-readable-canary").read_bytes())

            stage = "deny-tmp-write"
            denied(
                lambda: Path("/tmp/outside-sandbox-write").write_bytes(b"should be denied")
            )

            stage = "deny-data"
            denied(lambda: Path("/data/validator-canary").read_bytes())

            stage = "deny-homeassistant"
            denied(lambda: Path("/homeassistant/configuration.yaml").read_bytes())

        if group in {"all", "network"}:
            stage = "deny-ipv4"
            denied(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))

            stage = "deny-ipv6"
            denied(lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM))

        if group in {"all", "exec"}:
            stage = "deny-other-exec"
            denied(lambda: os.execv("/bin/true", ["/bin/true"]))

            # Core may relaunch only this dedicated interpreter for dependency-site discovery.
            stage = "allow-validator-reexec"
            subprocess.run(
                ["/opt/syncapp-validator/python3", "-I", "-c", "raise SystemExit(0)"],
                check=True,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                timeout=10,
            )

        if group in {"all", "runtime-workspace"}:
            # asyncio's private wakeup pair and worker threads must remain usable.
            stage = "allow-thread"
            assert asyncio.run(asyncio.to_thread(lambda: 42)) == 42

            stage = "allow-workspace-io"
            (config / "disposable-registry.json").write_text("{}")
            assert (config / "disposable-registry.json").read_text() == "{}"
    except BaseException:
        # CI needs enough information to locate a confinement regression without ever
        # emitting exception text, candidate bytes, environment values or credentials.
        print(f"sandbox probe failed: {stage}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None

    print("sandbox verified")


def denied(operation) -> None:
    try:
        operation()
    except PermissionError:
        return
    raise AssertionError("Sandbox permitted an excluded operation")


if __name__ == "__main__":
    run()
