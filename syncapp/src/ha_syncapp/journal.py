"""Durable idempotent work records. Call only under StateStore's lifetime lock."""

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .errors import Failure


def migrate(db: sqlite3.Connection) -> None:
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE values_store (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "job_key TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, "
            "due REAL NOT NULL, phase TEXT NOT NULL, payload TEXT NOT NULL, "
            "error TEXT, created REAL NOT NULL, UNIQUE(kind, job_key))"
        )
        db.execute("CREATE INDEX jobs_due ON jobs(status, due)")
        db.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, time REAL NOT NULL, "
            "event TEXT NOT NULL, details TEXT NOT NULL)"
        )
        db.execute("PRAGMA user_version = 5")


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    key: str
    status: str
    attempts: int
    due: float
    phase: str
    payload: dict[str, Any]
    error: str | None
    created: float


class Journal:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def value(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM values_store WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO values_store VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def enqueue(self, kind: str, key: str, payload: dict[str, Any]) -> Job:
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO jobs VALUES (?, ?, ?, 'pending', 0, 0, 'new', ?, NULL, ?)",
                (str(uuid4()), kind, key, json.dumps(payload), time.time()),
            )
        row = self.db.execute(
            "SELECT * FROM jobs WHERE kind = ? AND job_key = ?", (kind, key)
        ).fetchone()
        job = self._job(row)
        if result.rowcount:
            self.record("enqueued", job=job.id, kind=kind)
        return job

    @staticmethod
    def _job(row: Any) -> Job:
        if row is None:
            raise Failure("job_missing")
        values = list(row)
        values[7] = json.loads(values[7])
        return Job(*values)

    def get(self, job_id: str) -> Job:
        return self._job(self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def jobs(self) -> list[Job]:
        return [self._job(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY created")]

    def claim(self, now: float | None = None, *, only_id: str | None = None) -> Job | None:
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT id FROM jobs WHERE status IN ('pending', 'retry') "
                "AND due <= ? AND (? IS NULL OR id = ?) ORDER BY created LIMIT 1",
                (now, only_id, only_id),
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1 WHERE id = ?",
                (row[0],),
            )
        job = self.get(row[0])
        self.record("attempt_started", job=job.id, kind=job.kind, attempt=job.attempts)
        return job

    def checkpoint(self, job_id: str, phase: str, updates: dict[str, Any]) -> None:
        payload = {**self.get(job_id).payload, **updates}
        with self.db:
            self.db.execute(
                "UPDATE jobs SET phase = ?, payload = ? WHERE id = ?",
                (phase, json.dumps(payload), job_id),
            )
        self.record("checkpoint", job=job_id, phase=phase)

    def succeed(self, job_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status = 'succeeded', error = NULL WHERE id = ?", (job_id,)
            )
        self.record("succeeded", job=job_id)

    def fail(self, job_id: str, error: Failure, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        job = self.get(job_id)
        retry = error.retryable and job.attempts < 8
        delay = min(3600, 30 * 2 ** min(max(job.attempts - 1, 0), 7))
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status = ?, due = ?, error = ? WHERE id = ?",
                ("retry" if retry else "blocked", now + delay, error.code, job_id),
            )
        self.record(
            "attempt_failed",
            job=job_id,
            error=error.code,
            retry=retry,
            attempt=job.attempts,
            due=now + delay,
        )

    def recover(self) -> int:
        with self.db:
            result = self.db.execute(
                "UPDATE jobs SET status = 'retry', due = 0 WHERE status = 'running'"
            )
        if result.rowcount:
            self.record("interrupted_jobs_recovered", count=result.rowcount)
        return result.rowcount

    def retry(self, job_id: str, *, now: float | None = None) -> None:
        job = self.get(job_id)
        if job.status not in ("blocked", "retry"):
            raise Failure("job_not_retryable")
        self.checkpoint(job_id, job.phase, {"explicit_retry": True})
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status = 'retry', attempts = 0, due = ?, error = NULL "
                "WHERE id = ?",
                (time.time() if now is None else now, job_id),
            )
        self.record("explicit_retry", job=job_id)

    def record(self, event: str, **details: Any) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events(time, event, details) VALUES (?, ?, ?)",
                (time.time(), event, json.dumps(details)),
            )

    def events(self, *, since: float = 0) -> list[dict[str, Any]]:
        return [
            {"time": row[0], "event": row[1], **json.loads(row[2])}
            for row in self.db.execute(
                "SELECT time, event, details FROM events WHERE time >= ? ORDER BY id", (since,)
            )
        ]

    def maintain(self, now: float) -> None:
        cutoff = now - 30 * 86400
        with self.db:
            self.db.execute(
                "DELETE FROM events WHERE time < ? OR id IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT -1 OFFSET 100000)",
                (cutoff,),
            )
            self.db.execute(
                "DELETE FROM jobs WHERE status = 'succeeded' AND kind != 'initialize' "
                "AND (created < ? OR id IN (SELECT id FROM jobs WHERE "
                "status = 'succeeded' AND kind != 'initialize' "
                "ORDER BY created DESC LIMIT -1 OFFSET 10000))",
                (cutoff,),
            )
