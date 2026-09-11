"""Load the Supervisor-adjusted shipped AppArmor policy on a native CI runner."""

import subprocess
import sys
from pathlib import Path

PROFILE = "ci_homeassistant_syncapp_v2"
POLICY = Path("/tmp/syncapp-ci-apparmor.txt")


def run() -> None:
    if sys.argv[1:] == ["load"]:
        source = Path(__file__).resolve().parent.parent / "syncapp/apparmor.txt"
        policy = source.read_text().replace(
            "profile homeassistant_syncapp_v2 ", f"profile {PROFILE} ", 1
        )
        POLICY.write_text(policy)
        mode = "-r"
    elif sys.argv[1:] == ["unload"]:
        if not POLICY.exists():
            return
        mode = "-R"
    else:
        raise SystemExit("Expected load or unload")
    subprocess.run(
        ["sudo", "/usr/sbin/apparmor_parser", mode, "-W", str(POLICY)],
        check=True,
        timeout=30,
    )
    if mode == "-R":
        POLICY.unlink()


if __name__ == "__main__":
    run()
