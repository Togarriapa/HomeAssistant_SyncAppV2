"""Local deploy keys. Only public material is returned to the control panel."""

import json
import os
import re
import shlex
import stat

# ssh-keygen receives fixed argv and an app-owned path, never a shell command.
import subprocess  # nosec B404
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import Failure
from .files import fsync_dir
from .journal import Journal

# Published by GitHub, not learned from the network on first use.
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
GITHUB_HOST = (
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
)


@dataclass(frozen=True)
class SSHIdentity:
    private_key: Path
    known_hosts: Path

    def command(self) -> str:
        return shlex.join(
            [
                "ssh",
                "-F",
                "/dev/null",
                "-i",
                str(self.private_key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={self.known_hosts}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "HostKeyAlgorithms=ssh-ed25519",
                "-o",
                "ConnectTimeout=15",
            ]
        )


def _private_file(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise Failure("unsafe_key_file")


class KeyManager:
    def __init__(self, directory: Path, journal: Journal) -> None:
        self.directory = directory
        self.journal = journal
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise Failure("unsafe_key_directory")
        directory.chmod(0o700)
        self.hosts = directory / "known_hosts"
        if not self.hosts.exists() and not self.hosts.is_symlink():
            fd = os.open(self.hosts, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(GITHUB_HOST)
                output.flush()
                os.fsync(output.fileno())
            fsync_dir(directory)
        _private_file(self.hosts)
        if self.hosts.read_text() != GITHUB_HOST:
            raise Failure("ssh_host_pin_changed")

    def _identity(self, record: dict[str, str] | None) -> SSHIdentity:
        if not record or not re.fullmatch(r"[a-f0-9]{32}", record.get("id", "")):
            raise Failure("deploy_key_missing")
        path = self.directory / record["id"]
        try:
            _private_file(path)
            _private_file(path.with_suffix(".pub"))
        except OSError:
            raise Failure("deploy_key_missing") from None
        return SSHIdentity(path, self.hosts)

    def identity(self) -> SSHIdentity:
        return self._identity(self.journal.value("key_active"))

    def generate(self) -> dict[str, str]:
        pending: dict[str, str] | None = self.journal.value("key_pending")
        if pending:
            self._identity(pending)
            return pending
        self._prune()
        identifier = uuid4().hex
        path = self.directory / identifier
        try:
            # Arguments are fixed except an app-owned UUID path; no shell or secrets in output.
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    f"SyncAppV2-{identifier}",
                    "-f",
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )  # nosec B603 B607
            path.chmod(0o600)
            public_path = path.with_suffix(".pub")
            public_path.chmod(0o600)
            public = public_path.read_text().strip()
            result = subprocess.run(
                ["ssh-keygen", "-l", "-E", "sha256", "-f", str(public_path)],
                check=True,
                capture_output=True,
                timeout=30,
            )  # nosec B603 B607
            fingerprint = result.stdout.decode().split()[1]
            for item in (path, public_path):
                with item.open("rb") as stream:
                    os.fsync(stream.fileno())
            fsync_dir(self.directory)
        except (OSError, subprocess.SubprocessError, UnicodeError, IndexError):
            raise Failure("key_generation_failed") from None
        pending = {"id": identifier, "public_key": public, "fingerprint": fingerprint}
        self.journal.set("key_pending", pending)
        self.journal.set("key_test", None)
        return pending

    def test(self, repository: str, tester: Callable[[SSHIdentity], None]) -> None:
        record = self.journal.value("key_pending") or self.journal.value("key_active")
        identity = self._identity(record)
        self.journal.set("key_test", None)
        tester(identity)
        self.journal.set(
            "key_test", {"id": record["id"], "repository": repository.lower(), "time": time.time()}
        )

    def activate(self, repository: str) -> None:
        pending = self.journal.value("key_pending")
        verified = self.journal.value("key_test")
        if (
            not pending
            or not verified
            or verified["id"] != pending["id"]
            or verified["repository"] != repository.lower()
            or not 0 <= time.time() - verified["time"] <= 600
        ):
            raise Failure("deploy_key_not_tested")
        self._identity(pending)
        # One transaction: power loss leaves either the old or the new active identity.
        with self.journal.db:
            for key, value in (
                ("key_previous", self.journal.value("key_active")),
                ("key_active", pending),
                ("key_pending", None),
            ):
                self.journal.db.execute(
                    "INSERT INTO values_store VALUES (?, ?) ON CONFLICT(key) "
                    "DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value)),
                )
        self._prune()

    def _prune(self) -> None:
        keep = {
            record["id"]
            for name in ("active", "pending", "previous")
            if (record := self.journal.value(f"key_{name}"))
        }
        try:
            for path in self.directory.iterdir():
                if re.fullmatch(r"[a-f0-9]{32}(\.pub)?", path.name) and path.stem not in keep:
                    path.unlink()
            fsync_dir(self.directory)
        except OSError:
            self.journal.record("key_cleanup_deferred")

    def status(self) -> dict[str, Any]:
        return {
            name: self.journal.value(f"key_{name}")
            for name in ("active", "pending", "previous", "test")
        }
