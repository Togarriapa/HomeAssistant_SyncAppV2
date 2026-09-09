"""Serialized, durable operations. No scheduled work before explicit initialization."""

import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import Failure
from .files import Snapshot, capture
from .git import GENERATED, GitRepository
from .homeassistant import HomeAssistant
from .journal import Job, Journal


class Engine:
    def __init__(
        self,
        directory: Path,
        config_root: Path,
        journal: Journal,
        repository: Callable[[], GitRepository],
        homeassistant: HomeAssistant,
        *,
        sync_interval: int = 60,
        retrigger_interval: int = 3600,
    ) -> None:
        self.directory = directory
        self.config_root = config_root
        self.journal = journal
        self.repository = repository
        self.homeassistant = homeassistant
        self.sync_interval = sync_interval
        self.next_sync = 0.0
        self.retrigger_interval = retrigger_interval
        self.next_retrigger = 0.0

    def request_initialize(self) -> Job:
        return self.journal.enqueue("initialize", "initial", {})

    def _schedule(self, now: float) -> None:
        if now < self.next_sync:
            return
        self.next_sync = now + self.sync_interval
        previous = [j for j in self.journal.jobs() if j.kind == "sync"]
        if previous and previous[-1].status != "succeeded":
            return
        self.journal.enqueue("sync", str(int(now)), {})

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        initialized = self.journal.value("initialized") is True
        if now >= self.next_retrigger:
            self.next_retrigger = now + self.retrigger_interval
            self.journal.recover()
            self.journal.record(
                "retrigger_scan" if initialized else "retrigger_skipped",
                pending=sum(j.status in ("pending", "retry") for j in self.journal.jobs()),
                blocked=sum(j.status == "blocked" for j in self.journal.jobs()),
            )
            self.journal.maintain(now)
        if initialized:
            self._schedule(now)
        pending = [
            j
            for j in self.journal.jobs()
            if j.status in ("pending", "retry")
            and j.due <= now
            and (initialized or j.kind == "initialize")
        ]
        if not pending:
            return
        job = self.journal.claim(now, only_id=pending[0].id)
        if job is None:
            return
        try:
            if job.kind == "initialize":
                self._initialize(job)
            elif job.kind == "sync":
                self._sync(job)
            else:
                raise Failure("unsupported_operation")
            self.journal.succeed(job.id)
            snapshot_path = self.directory / "snapshots" / job.id
            try:
                if snapshot_path.exists():
                    shutil.rmtree(snapshot_path)
            except OSError:
                self.journal.record("snapshot_cleanup_deferred", job=job.id)
            self.journal.set("last_success", {"kind": job.kind, "job": job.id, "time": now})
        except Failure as error:
            self.journal.fail(job.id, error, now=now)
        except (OSError, ValueError, KeyError, TypeError):
            self.journal.fail(job.id, Failure("operation_state_invalid"), now=now)

    def _initialize(self, job: Job) -> None:
        if self.journal.value("initialized") is True:
            return
        repo = self.repository()
        self.homeassistant.health()
        if job.phase == "new":
            repo.test_access(job.id.replace("-", ""))
            if repo.remote_refs():
                raise Failure("repository_not_empty")
            snapshot = capture(self.config_root)
            snapshot.save(self.directory / "snapshots" / job.id)
            initial = repo.commit(
                snapshot, parent=None, message=f"Initialize {job.id}", timestamp=int(job.created)
            )
            commits = {"main": initial, "candidate": initial}
            for branch in sorted(GENERATED):
                manifest = Snapshot(
                    {
                        "manifest.json": json.dumps(
                            {
                                "version": 1,
                                "branch": branch,
                                "status": "awaiting_first_snapshot",
                            },
                            sort_keys=True,
                        ).encode()
                        + b"\n"
                    }
                )
                commits[branch] = repo.commit(
                    manifest,
                    parent=None,
                    message=f"Initialize {branch} {job.id}",
                    timestamp=int(job.created),
                )
            self.journal.checkpoint(
                job.id, "planned", {"commits": commits, "digest": snapshot.digest}
            )
            job = self.journal.get(job.id)
        snapshot = Snapshot.load(self.directory / "snapshots" / job.id)
        if snapshot.digest != job.payload["digest"]:
            raise Failure("snapshot_corrupt")
        commits = job.payload["commits"]
        if repo.snapshot(commits["main"]).digest != snapshot.digest:
            raise Failure("snapshot_corrupt")
        repo.initialize(commits)
        self.journal.set("main", commits["main"])
        self.journal.set("synced_digest", snapshot.digest)
        self.journal.set("candidate_seen", commits["candidate"])
        self.journal.set("initialized", True)  # Last: no other work until all refs are confirmed.

    def _sync(self, job: Job) -> None:
        if self.journal.value("initialized") is not True:
            raise Failure("repository_not_initialized")
        repo = self.repository()
        if job.phase == "planned":
            # Reconcile the saved commit before considering any new local edits.
            saved = Snapshot.load(self.directory / "snapshots" / job.id)
            if (
                saved.digest != job.payload["digest"]
                or repo.snapshot(job.payload["commit"]).digest != saved.digest
            ):
                raise Failure("snapshot_corrupt")
            repo.publish("main", job.payload["commit"], expected=job.payload["expected"])
            self.journal.set("main", job.payload["commit"])
            self.journal.set("synced_digest", job.payload["digest"])
            return
        expected = self.journal.value("main")
        if repo.ref("main") != expected:
            raise Failure("remote_main_changed")
        candidate = repo.ref("candidate")
        self.journal.set("candidate_seen", candidate)
        if candidate is None:
            raise Failure("candidate_branch_missing")
        if not repo.is_ancestor(candidate, expected):
            raise Failure("candidate_requires_processing")
        snapshot = capture(self.config_root)
        if snapshot.digest == self.journal.value("synced_digest"):
            self.journal.set("debounce_digest", None)
            return
        previous = self.journal.value("debounce_digest")
        self.journal.set("debounce_digest", snapshot.digest)
        if previous != snapshot.digest:
            return
        self.homeassistant.health()
        snapshot.save(self.directory / "snapshots" / job.id)
        commit = repo.commit(
            snapshot,
            parent=expected,
            message=f"Local snapshot {job.id}",
            timestamp=int(job.created),
        )
        self.journal.checkpoint(
            job.id, "planned", {"commit": commit, "expected": expected, "digest": snapshot.digest}
        )
        repo.publish("main", commit, expected=expected)
        self.journal.set("main", commit)
        self.journal.set("synced_digest", snapshot.digest)

    def status(self) -> dict[str, Any]:
        return {
            "initialized": self.journal.value("initialized") is True,
            "main": self.journal.value("main"),
            "candidate": self.journal.value("candidate_seen"),
            "last_success": self.journal.value("last_success"),
            "jobs": [
                {
                    "id": j.id,
                    "kind": j.kind,
                    "status": j.status,
                    "phase": j.phase,
                    "attempts": j.attempts,
                    "due": j.due,
                    "error": j.error,
                }
                for j in self.journal.jobs()[-30:][::-1]
            ],
        }
