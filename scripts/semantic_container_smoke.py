"""Run a selected semantic check in the actual app image on a native CI runner."""

import subprocess
import sys
from pathlib import Path


def run() -> None:
    root = Path(__file__).resolve().parent.parent
    check_script = sys.argv[1] if len(sys.argv) == 2 else "semantic_image_check.py"
    if "/" in check_script or not check_script.endswith(".py"):
        raise SystemExit("invalid semantic smoke script")
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--init",
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=256m",
            "--volume",
            f"{root / 'scripts'}:/checks:ro",
            "--volume",
            f"{root / 'tests'}:/fixtures:ro",
            "--entrypoint",
            "/opt/syncapp/bin/python",
            "syncapp:test",
            f"/checks/{check_script}",
        ],
        check=True,
        timeout=600,
    )


if __name__ == "__main__":
    run()
