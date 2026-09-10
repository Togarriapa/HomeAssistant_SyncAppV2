"""Container-only sandbox boundary probe using no Home Assistant installation data."""

import os
import subprocess
import tempfile
from pathlib import Path


def run() -> None:
    canary = Path("/tmp/world-readable-canary")
    canary.write_text("credential-canary")
    canary.chmod(0o644)
    with tempfile.TemporaryDirectory(prefix="sandbox-probe-") as directory:
        probe_root = Path(directory)
        config = probe_root / "config"
        config.mkdir(mode=0o700)
        os.chown(config, 65534, 65534)
        os.chown(probe_root, 65534, 65534)
        output = subprocess.check_output(
            [
                "/usr/local/bin/python3",
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


if __name__ == "__main__":
    run()
