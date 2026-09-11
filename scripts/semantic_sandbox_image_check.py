"""Container-only sandbox boundary probe using no Home Assistant installation data."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_GROUPS = {"all", "identity", "filesystem", "network", "exec", "runtime-workspace"}


def run() -> None:
    group = sys.argv[1] if len(sys.argv) == 2 else "all"
    if group not in _GROUPS:
        raise SystemExit("invalid semantic sandbox group")

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
                group,
            ],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            text=True,
            timeout=30,
        )
        assert output.strip() == "sandbox verified", output


if __name__ == "__main__":
    run()
