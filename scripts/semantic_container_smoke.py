"""Run a selected semantic check in the actual app image on a native CI runner."""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ARGUMENT = re.compile(r"[a-z][a-z0-9-]{0,63}")


def run() -> None:
    root = Path(__file__).resolve().parent.parent
    check_script = sys.argv[1] if len(sys.argv) >= 2 else "semantic_image_check.py"
    if "/" in check_script or not check_script.endswith(".py"):
        raise SystemExit("invalid semantic smoke script")

    check_args: list[str] = []
    if len(sys.argv) == 3:
        if _ARGUMENT.fullmatch(sys.argv[2]) is None:
            raise SystemExit("invalid semantic smoke argument")
        check_args.append(sys.argv[2])
    elif len(sys.argv) > 3:
        raise SystemExit("too many semantic smoke arguments")

    with (
        tempfile.TemporaryDirectory(prefix="syncapp-ci-data-") as data_dir,
        tempfile.TemporaryDirectory(prefix="syncapp-ci-homeassistant-") as ha_dir,
    ):
        data = Path(data_dir)
        homeassistant = Path(ha_dir)
        (data / "validator-canary").write_text("synthetic-data-canary")
        (homeassistant / "configuration.yaml").write_text("synthetic-ha-canary")
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--init",
                "--read-only",
                "--security-opt",
                "apparmor=ci_homeassistant_syncapp_v2",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=256m",
                "--volume",
                f"{root / 'scripts'}:/checks:ro",
                "--volume",
                f"{root / 'tests'}:/fixtures:ro",
                "--volume",
                f"{data}:/data:ro",
                "--volume",
                f"{homeassistant}:/homeassistant:ro",
                "--entrypoint",
                "/opt/syncapp/bin/python",
                "syncapp:test",
                f"/checks/{check_script}",
                *check_args,
            ],
            check=True,
            timeout=600,
        )


if __name__ == "__main__":
    run()
