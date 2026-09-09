"""Raw Git object transport with atomic compare-and-swap ref updates."""

import os
import re

# Commands below use fixed git argv, a sanitized environment, and no shell.
import subprocess  # nosec B404
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import Failure
from .files import MAX_BYTES, MAX_FILES, Snapshot, valid_path
from .keys import SSHIdentity

BRANCHES = frozenset({"main", "candidate", "database", "runtime", "logs"})
GENERATED = frozenset({"database", "runtime", "logs"})
SHA = re.compile(r"[0-9a-f]{40}")


def check_sha(sha: str) -> str:
    if not SHA.fullmatch(sha):
        raise Failure("invalid_commit")
    return sha


class GitRepository:
    def __init__(
        self,
        directory: Path,
        remote: str,
        *,
        identity: SSHIdentity | None = None,
        guard: Callable[[], None] | None = None,
    ) -> None:
        if re.fullmatch(r"git@github.com:[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\.git", remote):
            if guard is None or identity is None:
                raise Failure("ssh_credentials_missing")
        elif not Path(remote).is_absolute():
            raise Failure("unsupported_git_remote")
        self.directory = directory
        self.remote = remote
        self._guard = guard
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Home Assistant SyncApp",
            "GIT_AUTHOR_EMAIL": "syncapp@localhost",
            "GIT_COMMITTER_NAME": "Home Assistant SyncApp",
            "GIT_COMMITTER_EMAIL": "syncapp@localhost",
            "LC_ALL": "C",
        }
        if identity is not None:
            self.env["GIT_SSH_COMMAND"] = identity.command()
        if not directory.exists():
            self.run("init", "--bare", str(directory), bare=False)
        if directory.is_symlink() or not (directory / "HEAD").is_file():
            raise Failure("unsafe_git_directory")
        directory.chmod(0o700)

    def run(
        self, *args: str, data: bytes | None = None, bare: bool = True, timestamp: int | None = None
    ) -> bytes:
        env = dict(self.env)
        if timestamp is not None:
            env.update(
                GIT_AUTHOR_DATE=f"@{timestamp} +0000", GIT_COMMITTER_DATE=f"@{timestamp} +0000"
            )
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "gc.auto=0",
            "-c",
            "transfer.fsckObjects=true",
            "-c",
            "http.followRedirects=false",
            "-c",
            "credential.helper=",
        ]
        if bare:
            command += ["--git-dir", str(self.directory)]
        try:
            result = subprocess.run(
                command + list(args),
                input=data,
                capture_output=True,
                env=env,
                timeout=120,
                check=False,
            )  # nosec B603
        except (OSError, subprocess.TimeoutExpired):
            raise Failure("git_unavailable", retryable=True) from None
        if result.returncode:
            raise Failure("git_operation_failed", retryable=True)
        return result.stdout

    def _verify(self) -> None:
        if self._guard is not None:
            self._guard()

    def _remote_ref(self, ref: str) -> str | None:
        self._verify()
        output = self.run("ls-remote", "--refs", self.remote, ref)
        lines = output.decode().splitlines()
        if not lines:
            return None
        if len(lines) != 1 or lines[0].split("\t")[1] != ref:
            raise Failure("remote_ref_invalid")
        return check_sha(lines[0].split("\t")[0])

    def ref(self, branch: str) -> str | None:
        if branch not in BRANCHES:
            raise Failure("unsupported_branch")
        sha = self._remote_ref(f"refs/heads/{branch}")
        if sha is not None:
            self._verify()
            self.run("fetch", "--no-tags", "--no-write-fetch-head", self.remote, sha)
            self.run("update-ref", f"refs/remotes/syncapp/{branch}", sha)
        return sha

    def snapshot(self, commit: str, *, max_bytes: int = MAX_BYTES) -> Snapshot:
        check_sha(commit)
        rows = self.run("ls-tree", "-rz", "--full-tree", commit).split(b"\0")
        files: dict[str, bytes] = {}
        total = 0
        for row in rows:
            if not row:
                continue
            metadata, raw_name = row.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if mode not in (b"100644", b"100755") or kind != b"blob":
                raise Failure("unsafe_git_entry")
            try:
                name = valid_path(raw_name.decode("utf-8"))
            except UnicodeError:
                raise Failure("unsafe_path") from None
            size = int(self.run("cat-file", "-s", oid.decode()).strip())
            total += size
            if total > max_bytes or len(files) >= MAX_FILES:
                raise Failure("snapshot_too_large")
            files[name] = self.run("cat-file", "blob", oid.decode())
        return Snapshot(files)

    def commit(
        self, snapshot: Snapshot, *, parent: str | None, message: str, timestamp: int
    ) -> str:
        tree: dict[str, Any] = {}
        for name, data in snapshot.files.items():
            node = tree
            parts = valid_path(name).split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = self.run(
                "hash-object", "-w", "--no-filters", "--stdin", data=data
            ).strip()

        def write_tree(node: dict[str, Any]) -> str:
            entries = []
            for name, value in node.items():
                if isinstance(value, dict):
                    entry = b"040000 tree " + write_tree(value).encode()
                else:
                    entry = b"100644 blob " + value
                entries.append(entry + b"\t" + name.encode() + b"\0")
            return self.run("mktree", "-z", data=b"".join(entries)).strip().decode()

        args = ["commit-tree", write_tree(tree)]
        if parent is not None:
            args += ["-p", check_sha(parent)]
        return check_sha(
            self.run(*args, data=message.encode(), timestamp=timestamp).strip().decode()
        )

    def publish(
        self, branch: str, commit: str, *, expected: str | None, generated: bool = False
    ) -> None:
        if branch not in BRANCHES or (generated and branch not in GENERATED):
            raise Failure("unsupported_branch")
        check_sha(commit)
        ref = f"refs/heads/{branch}"
        current = self._remote_ref(ref)
        if current == commit:
            return
        if current != expected:
            raise Failure("remote_conflict")
        if expected is not None and not generated and not self.is_ancestor(expected, commit):
            raise Failure("non_fast_forward")
        self._verify()
        try:
            self.run(
                "push",
                "--porcelain",
                f"--force-with-lease={ref}:{expected or ''}",
                self.remote,
                f"{commit}:{ref}",
            )
        except Failure:
            after = self._remote_ref(ref)
            if after == commit:
                return
            if after != expected:
                raise Failure("remote_conflict") from None
            raise
        self.run("update-ref", f"refs/remotes/syncapp/{branch}", commit)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        try:
            self.run("merge-base", "--is-ancestor", check_sha(ancestor), check_sha(descendant))
            return True
        except Failure:
            return False

    def remote_refs(self) -> dict[str, str]:
        self._verify()
        result = {}
        for line in self.run("ls-remote", "--refs", self.remote).decode().splitlines():
            parts = line.split("\t")
            if len(parts) != 2 or not parts[1].startswith("refs/"):
                raise Failure("remote_ref_invalid")
            result[parts[1]] = check_sha(parts[0])
        return result

    def initialize(self, commits: dict[str, str]) -> None:
        if set(commits) != BRANCHES:
            raise Failure("initial_branches_missing")
        planned = {f"refs/heads/{b}": check_sha(s) for b, s in commits.items()}
        current = self.remote_refs()
        if current == planned:
            return
        if current:
            raise Failure("repository_not_empty")
        self._verify()
        try:
            self.run(
                "push",
                "--atomic",
                "--porcelain",
                *(f"--force-with-lease={ref}:" for ref in sorted(planned)),
                self.remote,
                *(f"{planned[ref]}:{ref}" for ref in sorted(planned)),
            )
        except Failure:
            if self.remote_refs() == planned:
                return
            raise
        if self.remote_refs() != planned:
            raise Failure("initialization_not_confirmed", retryable=True)

    def test_access(self, identifier: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise Failure("invalid_key_test")
        # A tag does not become the default branch of an otherwise empty repository.
        ref = f"refs/tags/syncapp-key-test-{identifier}"
        commit = self.commit(
            Snapshot({"key-test.txt": b"SyncApp deploy key write test\n"}),
            parent=None,
            message="SyncApp key test",
            timestamp=1,
        )
        current = self._remote_ref(ref)
        if current not in (None, commit):
            raise Failure("key_test_ref_conflict")
        self._verify()
        try:
            self.run(
                "push", f"--force-with-lease={ref}:{current or ''}", self.remote, f"{commit}:{ref}"
            )
        except Failure:
            if self._remote_ref(ref) != commit:
                raise
        self._verify()
        try:
            self.run("push", f"--force-with-lease={ref}:{commit}", self.remote, f":{ref}")
        except Failure:
            if self._remote_ref(ref) is not None:
                raise
        if self._remote_ref(ref) is not None:
            raise Failure("key_test_cleanup_failed", retryable=True)

    def tag(self, name: str, commit: str) -> None:
        if not re.fullmatch(r"known-good/[a-zA-Z0-9_-]{1,100}", name):
            raise Failure("invalid_tag")
        check_sha(commit)
        ref = f"refs/tags/{name}"
        current = self._remote_ref(ref)
        if current == commit:
            return
        if current is not None:
            raise Failure("tag_conflict")
        self._verify()
        self.run("push", f"--force-with-lease={ref}:", self.remote, f"{commit}:{ref}")
