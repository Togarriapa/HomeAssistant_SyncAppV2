"""Exercise the actual image with persisted /data and isolated networking."""

import json
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True, timeout=30).strip()


def wait_for_start(name: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for line in docker("logs", name).splitlines():
            event = json.loads(line)
            if event["event"] == "service_started":
                return event
            if event["event"] == "service_failed":
                raise RuntimeError(f"Container failed: {event['reason']}")
        time.sleep(0.1)
    raise RuntimeError("Container did not start")


def run() -> None:
    name = f"syncapp-smoke-{uuid4().hex}"
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory)
        (data / "options.json").write_text("{}")
        first = None
        for boot_count in (1, 2):
            try:
                docker(
                    "run",
                    "--detach",
                    "--init",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--user",
                    f"{data.stat().st_uid}:{data.stat().st_gid}",
                    "--volume",
                    f"{data}:/data",
                    "syncapp:test",
                )
                started = wait_for_start(name)
                assert started["mode"] == "passive"
                assert started["boot_count"] == boot_count
                assert started["interrupted_run_id"] is None
                if first is not None:
                    assert started["installation_id"] == first["installation_id"]
                first = started
                docker("stop", "--time", "5", name)
                assert docker("inspect", "--format", "{{.State.ExitCode}}", name) == "0"
                events = [json.loads(line) for line in docker("logs", name).splitlines()]
                assert events[-1]["event"] == "service_stopped"
            finally:
                subprocess.run(["docker", "rm", "--force", name], check=False, timeout=30)
        print("Container startup, shutdown and persistent identity verified")
        try:
            docker(
                "run",
                "--detach",
                "--init",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                f"{data.stat().st_uid}:{data.stat().st_gid}",
                "--env",
                "SUPERVISOR_TOKEN=synthetic-offline-token",
                "--volume",
                f"{data}:/data",
                "syncapp:test",
            )
            assert wait_for_start(name)["mode"] == "setup"
            probe = """
import json, urllib.request, urllib.error
from pathlib import Path
from ha_syncapp.state import StateStore
from ha_syncapp.journal import Journal
from ha_syncapp.keys import KeyManager
from ha_syncapp.git import GitRepository
from ha_syncapp.files import Snapshot
root = Path('/data/offline-probe')
root.mkdir()
with StateStore(root) as state:
    journal = Journal(state.connection)
    keys = KeyManager(root / 'syncapp/keys', journal)
    assert keys.generate()['public_key'].startswith('ssh-ed25519 ')
    remote = GitRepository(root / 'remote.git', str(root / 'unused.git'))
    repo = GitRepository(root / 'stage.git', str(remote.directory))
    repo.test_access('a' * 32)
    assert repo.remote_refs() == {}
    commit = repo.commit(Snapshot({'configuration.yaml': b'test:\\r\\n'}),
                         parent=None, message='offline container probe', timestamp=1)
    repo.publish('main', commit, expected=None)
    assert repo.snapshot(repo.ref('main')).files['configuration.yaml'] == b'test:\\r\\n'
try:
    urllib.request.urlopen('http://127.0.0.1:8099/status', timeout=5)
except urllib.error.HTTPError as error:
    assert error.code == 403
else:
    raise AssertionError('non-ingress request accepted')
print('Offline setup, Ed25519 key generation, raw Git transfer and ingress denial verified')
"""
            print(docker("exec", name, "python", "-c", probe))
            docker("stop", "--time", "5", name)
            assert docker("inspect", "--format", "{{.State.ExitCode}}", name) == "0"
        finally:
            subprocess.run(["docker", "rm", "--force", name], check=False, timeout=30)


if __name__ == "__main__":
    run()
