"""Exercise the production sandbox in a disposable child, never the test runner."""

import asyncio
import os
import runpy
import socket
import sys
from pathlib import Path

helper = runpy.run_path("/app/ha_syncapp/_validator_child.py", run_name="sandbox_probe")
config = Path(sys.argv[1])
helper["_sandbox"](config)
assert os.geteuid() != 0 and os.getuid() != 0
# The validator must retain only the fixed identity evidence Core needs to recognize
# the bundled official image; arbitrary files outside the sandbox remain inaccessible.
assert Path("/OFFICIAL_IMAGE").is_file()


def denied(operation):
    try:
        operation()
    except PermissionError:
        return
    raise AssertionError("Sandbox permitted an excluded operation")


denied(lambda: Path("/tmp/world-readable-canary").read_bytes())
denied(lambda: Path("/tmp/outside-sandbox-write").write_bytes(b"should be denied"))
denied(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
denied(lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM))
denied(lambda: os.execv("/bin/true", ["/bin/true"]))
# asyncio's private wakeup pair and worker threads still function.
assert asyncio.run(asyncio.to_thread(lambda: 42)) == 42
(config / "disposable-registry.json").write_text("{}")
assert (config / "disposable-registry.json").read_text() == "{}"
print("sandbox verified")
