"""Run real Core and sandbox checks in the actual app image on each native CI runner."""

import subprocess
from pathlib import Path


def run() -> None:
    root = Path(__file__).resolve().parent.parent
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
            "/checks/semantic_image_check.py",
        ],
        check=True,
        timeout=600,
    )


if __name__ == "__main__":
    run()
