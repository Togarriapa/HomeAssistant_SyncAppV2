"""Exercise the production sandbox in a disposable child, never the test runner."""

import asyncio
import ctypes
import os
import runpy
import socket
import subprocess
import sys
from functools import partial
from pathlib import Path

_GROUPS = {
    "all",
    "profile",
    "sandbox-entry",
    "identity",
    "image-marker",
    "filesystem",
    "network",
    "exec",
    "runtime-workspace",
}


def run() -> None:
    group = sys.argv[2] if len(sys.argv) == 3 else "all"
    if group not in _GROUPS:
        raise SystemExit("invalid sandbox probe group")

    stage = "load-helper"
    try:
        helper = runpy.run_path("/app/ha_syncapp/_validator_child.py", run_name="sandbox_probe")
        config = Path(sys.argv[1])

        if group == "profile":
            stage = "apparmor-profile"
            helper["_verify_apparmor_profile"]()
            print("sandbox verified")
            return

        stage = "enter-sandbox"
        helper["_sandbox"](config)

        if group == "sandbox-entry":
            print("sandbox verified")
            return

        if group in {"all", "identity"}:
            stage = "non-root"
            assert os.geteuid() != 0 and os.getuid() != 0

            stage = "no-new-privileges"
            assert ctypes.CDLL(None).prctl(39, 0, 0, 0, 0) == 1

            stage = "apparmor-label"
            profile = Path("/proc/self/attr/current").read_bytes().strip()
            assert profile == b"ci_homeassistant_syncapp_v2//validator (enforce)"

        if group in {"all", "image-marker"}:
            stage = "official-image"
            assert Path("/OFFICIAL_IMAGE").is_file()

        if group in {"all", "filesystem"}:
            stage = "deny-tmp-read"
            denied(Path("/tmp/world-readable-canary").read_bytes)

            stage = "deny-tmp-write"
            denied(partial(Path("/tmp/outside-sandbox-write").write_bytes, b"should be denied"))

            stage = "deny-data"
            denied(Path("/data/validator-canary").read_bytes)

            stage = "deny-homeassistant"
            denied(Path("/homeassistant/configuration.yaml").read_bytes)

        if group in {"all", "network"}:
            stage = "deny-ipv4"
            denied(partial(socket.socket, socket.AF_INET, socket.SOCK_STREAM))

            stage = "deny-ipv6"
            denied(partial(socket.socket, socket.AF_INET6, socket.SOCK_DGRAM))

        if group in {"all", "exec"}:
            stage = "deny-other-exec"
            denied(partial(os.execv, "/bin/true", ["/bin/true"]))

            stage = "allow-validator-reexec"
            command = ["/opt/syncapp-validator/python3", "-I", "-c", "raise SystemExit(0)"]
            subprocess.run(
                command,
                check=True,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                timeout=10,
            )

        if group in {"all", "runtime-workspace"}:
            stage = "allow-thread"
            assert asyncio.run(asyncio.to_thread(lambda: 42)) == 42

            stage = "allow-workspace-io"
            registry = config / "disposable-registry.json"
            registry.write_text("{}")
            assert registry.read_text() == "{}"
    except BaseException:
        # Emit only a fixed stage token; never exception text or candidate data.
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
