"""Exclusive, versioned lifecycle and recoverable-work state in app-owned storage.

The process lock is held until the SQLite connection closes. Its file must never
be deleted to recover a lock: the kernel releases flock when the process exits.
"""

import fcntl
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from uuid import UUID, uuid4

SCHEMA_VERSION = 4
_WORK_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class StateError(RuntimeError):
    """State is unsafe, unavailable, corrupt or unsupported."""


class AlreadyRunning(StateError):
    """Another process holds the lifetime lock."""


@dataclass(frozen=True)
class Boot:
    installation_id: str
    run_id: str
    boot_count: int
    interrupted_run_id: str | None


@dataclass(frozen=True)
class WorkItem:
    """Durable identity and lifecycle metadata for one idempotent unit of work."""

    work_kind: str
    work_key: str
    status: str
    attempts: int
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime | None


@dataclass(frozen=True)
class SynchronizationBaseline:
    """Last successful local synchronization evidence for one repository branch."""

    target: str
    branch: str
    snapshot_id: str
    commit_sha: str
    synchronized_at: datetime


def _private_file(path: Path, *, create: bool = False) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise StateError("Unsafe state file")
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _timestamp(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise StateError("State timestamps must include a timezone")
    return current.astimezone(UTC)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise StateError("Invalid state timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise StateError("Invalid state timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateError("Invalid state timestamp")
    return parsed.astimezone(UTC)


def _validate_work_identity(work_kind: str, work_key: str) -> None:
    if not isinstance(work_kind, str) or _WORK_KIND.fullmatch(work_kind) is None:
        raise StateError("Invalid work kind")
    if (
        not isinstance(work_key, str)
        or not 1 <= len(work_key) <= 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in work_key)
    ):
        raise StateError("Invalid work key")


def _validate_repository_binding(target: str, repository_id: int | None = None) -> None:
    if (
        not isinstance(target, str)
        or not 1 <= len(target) <= 200
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in target)
    ):
        raise StateError("Invalid repository binding")
    if repository_id is not None and (type(repository_id) is not int or repository_id <= 0):
        raise StateError("Invalid repository binding")


def _validate_branch(branch: str) -> None:
    forbidden = ("..", "//", "@{")
    if (
        not isinstance(branch, str)
        or not 1 <= len(branch) <= 200
        or branch.startswith(("/", "."))
        or branch.endswith(("/", ".", ".lock"))
        or any(token in branch for token in forbidden)
        or any(character.isspace() or character in "~^:?*[\\" for character in branch)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in branch)
    ):
        raise StateError("Invalid synchronization branch")


def _validate_synchronization_identity(
    target: str, branch: str, snapshot_id: str | None = None, commit_sha: str | None = None
) -> None:
    _validate_repository_binding(target)
    _validate_branch(branch)
    if snapshot_id is not None and (
        not isinstance(snapshot_id, str) or _HEX_64.fullmatch(snapshot_id) is None
    ):
        raise StateError("Invalid synchronization snapshot")
    if commit_sha is not None and (
        not isinstance(commit_sha, str) or _COMMIT_SHA.fullmatch(commit_sha) is None
    ):
        raise StateError("Invalid synchronization commit")


class StateStore:
    """One service instance owns one store for its entire lifetime."""

    MAX_WORK_ATTEMPTS = 8
    MAX_WORK_BACKOFF_SECONDS = 3600

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._root = data_dir / "syncapp"
        self._lock_fd: int | None = None
        self._db: sqlite3.Connection | None = None
        self._active_run: str | None = None
        self._started = False

    def __enter__(self) -> "StateStore":
        if self._db is not None or self._lock_fd is not None or self._started:
            raise StateError("Store cannot be reopened")
        try:
            self._prepare_directory()
            lock_path = self._root / "instance.lock"
            try:
                self._lock_fd = _private_file(lock_path, create=True)
            except FileExistsError:
                self._lock_fd = _private_file(lock_path)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AlreadyRunning("Another instance is running") from None
            self._open_database()
            return self
        except BaseException as error:
            self._close()
            if isinstance(error, (OSError, sqlite3.Error)):
                raise StateError("Unable to open protected state") from None
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._close()

    def _prepare_directory(self) -> None:
        if not stat.S_ISDIR(self._data_dir.lstat().st_mode):
            raise StateError("Data root must be a directory")
        self._root.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_uid != os.geteuid():
                raise StateError("State directory has a different owner")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
        parent_fd = os.open(self._data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    @property
    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise StateError("State is not open")
        return self._db

    @staticmethod
    def _create_work_table(db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE work ("
            "work_kind TEXT NOT NULL, work_key TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK (status IN ("
            "'pending','running','retry','blocked','succeeded')), "
            "attempts INTEGER NOT NULL CHECK (attempts >= 0), "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_attempt_at TEXT, "
            "PRIMARY KEY (work_kind, work_key))"
        )
        db.execute("CREATE INDEX work_ready ON work(status, next_attempt_at, created_at)")

    @staticmethod
    def _create_repository_binding_table(db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE repository_binding ("
            "target TEXT PRIMARY KEY, "
            "repository_id INTEGER NOT NULL CHECK (repository_id > 0))"
        )

    @staticmethod
    def _create_synchronization_baseline_table(db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE synchronization_baseline ("
            "target TEXT NOT NULL, branch TEXT NOT NULL, snapshot_id TEXT NOT NULL, "
            "commit_sha TEXT NOT NULL, synchronized_at TEXT NOT NULL, "
            "PRIMARY KEY (target, branch))"
        )

    def _open_database(self) -> None:
        path = self._root / "state.sqlite3"
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if os.path.lexists(sidecar):
                if not os.path.lexists(path):
                    raise StateError("Database is missing but recovery files exist")
                os.close(_private_file(sidecar))
        created = False
        try:
            fd = _private_file(path, create=True)
            created = True
        except FileExistsError:
            fd = _private_file(path)
        os.close(fd)
        self._db = sqlite3.connect(path, timeout=5)
        db = self._connection
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise StateError("State integrity check failed")
        db.execute("PRAGMA synchronous = FULL")
        if created:
            if version != 0:
                raise StateError("Unsupported state schema")
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE installation ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "installation_id TEXT NOT NULL, "
                    "boot_count INTEGER NOT NULL CHECK (boot_count >= 0), "
                    "active_run_id TEXT, last_started_at TEXT, last_stopped_at TEXT)"
                )
                db.execute(
                    "INSERT INTO installation VALUES (1, ?, 0, NULL, NULL, NULL)",
                    (str(uuid4()),),
                )
                self._create_work_table(db)
                self._create_repository_binding_table(db)
                self._create_synchronization_baseline_table(db)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        else:
            if version not in {1, 2, 3, SCHEMA_VERSION}:
                raise StateError("Unsupported state schema")
            self._identity()
            if version == 1:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    self._create_work_table(db)
                    db.execute("PRAGMA user_version = 2")
                version = 2
            if version == 2:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    self._create_repository_binding_table(db)
                    db.execute("PRAGMA user_version = 3")
                version = 3
            if version == 3:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    self._create_synchronization_baseline_table(db)
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._identity()

    def _identity(self) -> tuple[str, int, str | None]:
        rows = self._connection.execute(
            "SELECT installation_id, boot_count, active_run_id "
            "FROM installation WHERE singleton = 1"
        ).fetchall()
        if len(rows) != 1:
            raise StateError("Installation identity is missing")
        installation_id, boot_count, active_run = rows[0]
        if (
            not _valid_uuid(installation_id)
            or type(boot_count) is not int
            or boot_count < 0
            or (active_run is not None and not _valid_uuid(active_run))
        ):
            raise StateError("Invalid installation identity")
        return installation_id, boot_count, active_run

    def start_run(self) -> Boot:
        if self._started:
            raise StateError("Run already started")
        db = self._connection
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                installation_id, boot_count, interrupted = self._identity()
                run_id = str(uuid4())
                db.execute(
                    "UPDATE installation SET boot_count = ?, active_run_id = ?, "
                    "last_started_at = ? WHERE singleton = 1",
                    (boot_count + 1, run_id, datetime.now(UTC).isoformat()),
                )
            self._started = True
            self._active_run = run_id
            return Boot(installation_id, run_id, boot_count + 1, interrupted)
        except sqlite3.Error:
            raise StateError("Unable to record startup") from None

    def finish_run(self) -> None:
        if self._active_run is None:
            raise StateError("No active run")
        try:
            with self._connection as db:
                result = db.execute(
                    "UPDATE installation SET active_run_id = NULL, last_stopped_at = ? "
                    "WHERE singleton = 1 AND active_run_id = ?",
                    (datetime.now(UTC).isoformat(), self._active_run),
                )
                if result.rowcount != 1:
                    raise StateError("Active run changed unexpectedly")
            self._active_run = None
        except sqlite3.Error:
            raise StateError("Unable to record shutdown") from None

    def repository_id(self, target: str) -> int | None:
        """Return the pinned GitHub repository ID for a target, if already verified."""
        _validate_repository_binding(target)
        try:
            rows = self._connection.execute(
                "SELECT repository_id FROM repository_binding WHERE target = ?", (target,)
            ).fetchall()
        except sqlite3.Error:
            raise StateError("Unable to read repository binding") from None
        if not rows:
            return None
        if len(rows) != 1:
            raise StateError("Invalid repository binding")
        repository_id = rows[0][0]
        if type(repository_id) is not int or repository_id <= 0:
            raise StateError("Invalid repository binding")
        return repository_id

    def bind_repository(self, target: str, repository_id: int) -> None:
        """Pin a verified repository ID; reject replacement at the same target."""
        _validate_repository_binding(target, repository_id)
        try:
            with self._connection as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT repository_id FROM repository_binding WHERE target = ?", (target,)
                ).fetchone()
                if current is None:
                    db.execute(
                        "INSERT INTO repository_binding (target, repository_id) VALUES (?, ?)",
                        (target, repository_id),
                    )
                elif len(current) != 1 or current[0] != repository_id:
                    raise StateError("Repository identity changed unexpectedly")
        except sqlite3.Error:
            raise StateError("Unable to persist repository binding") from None

    def synchronization_baseline(
        self, target: str, branch: str
    ) -> SynchronizationBaseline | None:
        """Read the last successful local synchronization result for a branch."""
        _validate_synchronization_identity(target, branch)
        try:
            rows = self._connection.execute(
                "SELECT target, branch, snapshot_id, commit_sha, synchronized_at "
                "FROM synchronization_baseline WHERE target = ? AND branch = ?",
                (target, branch),
            ).fetchall()
        except sqlite3.Error:
            raise StateError("Unable to read synchronization baseline") from None
        if not rows:
            return None
        if len(rows) != 1:
            raise StateError("Invalid synchronization baseline")
        return self._synchronization_baseline_from_row(rows[0])

    def record_synchronization_baseline(
        self,
        target: str,
        branch: str,
        snapshot_id: str,
        commit_sha: str,
        *,
        synchronized_at: datetime | None = None,
    ) -> SynchronizationBaseline:
        """Atomically record a successful local synchronization result."""
        _validate_synchronization_identity(target, branch, snapshot_id, commit_sha)
        when = _timestamp(synchronized_at)
        try:
            with self._connection as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO synchronization_baseline "
                    "(target, branch, snapshot_id, commit_sha, synchronized_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(target, branch) DO UPDATE SET "
                    "snapshot_id = excluded.snapshot_id, commit_sha = excluded.commit_sha, "
                    "synchronized_at = excluded.synchronized_at",
                    (target, branch, snapshot_id, commit_sha, when.isoformat()),
                )
            baseline = self.synchronization_baseline(target, branch)
            if baseline is None:
                raise StateError("Synchronization baseline was not persisted")
            return baseline
        except sqlite3.Error:
            raise StateError("Unable to persist synchronization baseline") from None

    def _synchronization_baseline_from_row(
        self, row: tuple[object, ...]
    ) -> SynchronizationBaseline:
        if len(row) != 5:
            raise StateError("Invalid synchronization baseline")
        target, branch, snapshot_id, commit_sha, synchronized_at = row
        if not all(isinstance(value, str) for value in row):
            raise StateError("Invalid synchronization baseline")
        assert isinstance(target, str)
        assert isinstance(branch, str)
        assert isinstance(snapshot_id, str)
        assert isinstance(commit_sha, str)
        _validate_synchronization_identity(target, branch, snapshot_id, commit_sha)
        return SynchronizationBaseline(
            target=target,
            branch=branch,
            snapshot_id=snapshot_id,
            commit_sha=commit_sha,
            synchronized_at=_parse_timestamp(synchronized_at),
        )

    def _work_from_row(self, row: tuple[object, ...]) -> WorkItem:
        if len(row) != 7:
            raise StateError("Invalid work record")
        work_kind, work_key, status, attempts, created_at, updated_at, next_attempt_at = row
        if (
            not isinstance(work_kind, str)
            or not isinstance(work_key, str)
            or status not in {"pending", "running", "retry", "blocked", "succeeded"}
            or type(attempts) is not int
            or attempts < 0
        ):
            raise StateError("Invalid work record")
        _validate_work_identity(work_kind, work_key)
        return WorkItem(
            work_kind=work_kind,
            work_key=work_key,
            status=status,
            attempts=attempts,
            created_at=_parse_timestamp(created_at),
            updated_at=_parse_timestamp(updated_at),
            next_attempt_at=(
                None if next_attempt_at is None else _parse_timestamp(next_attempt_at)
            ),
        )

    def _get_work(self, work_kind: str, work_key: str) -> WorkItem:
        rows = self._connection.execute(
            "SELECT work_kind, work_key, status, attempts, created_at, updated_at, "
            "next_attempt_at FROM work WHERE work_kind = ? AND work_key = ?",
            (work_kind, work_key),
        ).fetchall()
        if len(rows) != 1:
            raise StateError("Work record is missing")
        return self._work_from_row(rows[0])

    def enqueue_work(
        self,
        work_kind: str,
        work_key: str,
        *,
        now: datetime | None = None,
    ) -> WorkItem:
        """Create one deterministic work item, or return its existing durable state."""
        _validate_work_identity(work_kind, work_key)
        current = _timestamp(now).isoformat()
        try:
            with self._connection as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO work (work_kind, work_key, status, attempts, "
                    "created_at, updated_at, next_attempt_at) "
                    "VALUES (?, ?, 'pending', 0, ?, ?, ?)",
                    (work_kind, work_key, current, current, current),
                )
            return self._get_work(work_kind, work_key)
        except sqlite3.Error:
            raise StateError("Unable to enqueue work") from None

    def claim_work(self, *, now: datetime | None = None) -> WorkItem | None:
        """Atomically claim the oldest eligible pending/retry item."""
        current = _timestamp(now).isoformat()
        try:
            with self._connection as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT work_kind, work_key, status, attempts, created_at, updated_at, "
                    "next_attempt_at FROM work WHERE status IN ('pending','retry') "
                    "AND next_attempt_at <= ? "
                    "ORDER BY next_attempt_at, created_at, work_kind, work_key LIMIT 1",
                    (current,),
                ).fetchone()
                if row is None:
                    return None
                item = self._work_from_row(row)
                result = db.execute(
                    "UPDATE work SET status = 'running', attempts = attempts + 1, "
                    "updated_at = ?, next_attempt_at = NULL "
                    "WHERE work_kind = ? AND work_key = ? AND status = ? AND attempts = ?",
                    (current, item.work_kind, item.work_key, item.status, item.attempts),
                )
                if result.rowcount != 1:
                    raise StateError("Work claim changed unexpectedly")
            return self._get_work(item.work_kind, item.work_key)
        except sqlite3.Error:
            raise StateError("Unable to claim work") from None

    def recover_interrupted_work(self, *, now: datetime | None = None) -> int:
        """Make work left running by an interrupted process immediately retryable."""
        current = _timestamp(now).isoformat()
        try:
            with self._connection as db:
                db.execute("BEGIN IMMEDIATE")
                result = db.execute(
                    "UPDATE work SET status = 'retry', updated_at = ?, next_attempt_at = ? "
                    "WHERE status = 'running'",
                    (current, current),
                )
            return result.rowcount
        except sqlite3.Error:
            raise StateError("Unable to recover interrupted work") from None

    def fail_work(
        self,
        item: WorkItem,
        *,
        transient: bool,
        now: datetime | None = None,
    ) -> WorkItem:
        """Record a failed running attempt as retryable or permanently blocked."""
        current_time = _timestamp(now)
        current = current_time.isoformat()
        if item.status != "running" or item.attempts < 1:
            raise StateError("Only running work can fail")
        if transient and item.attempts < self.MAX_WORK_ATTEMPTS:
            delay = min(60 * (2 ** (item.attempts - 1)), self.MAX_WORK_BACKOFF_SECONDS)
            status = "retry"
            next_attempt = (current_time + timedelta(seconds=delay)).isoformat()
        else:
            status = "blocked"
            next_attempt = None
        try:
            with self._connection as db:
                db.execute("BEGIN IMMEDIATE")
                result = db.execute(
                    "UPDATE work SET status = ?, updated_at = ?, next_attempt_at = ? "
                    "WHERE work_kind = ? AND work_key = ? AND status = 'running' "
                    "AND attempts = ?",
                    (status, current, next_attempt, item.work_kind, item.work_key, item.attempts),
                )
                if result.rowcount != 1:
                    raise StateError("Work transition changed unexpectedly")
            return self._get_work(item.work_kind, item.work_key)
        except sqlite3.Error:
            raise StateError("Unable to record work failure") from None

    def complete_work(self, item: WorkItem, *, now: datetime | None = None) -> WorkItem:
        """Durably mark one running attempt successful."""
        current = _timestamp(now).isoformat()
        if item.status != "running" or item.attempts < 1:
            raise StateError("Only running work can complete")
        try:
            with self._connection as db:
                db.execute("BEGIN IMMEDIATE")
                result = db.execute(
                    "UPDATE work SET status = 'succeeded', updated_at = ?, next_attempt_at = NULL "
                    "WHERE work_kind = ? AND work_key = ? AND status = 'running' "
                    "AND attempts = ?",
                    (current, item.work_kind, item.work_key, item.attempts),
                )
                if result.rowcount != 1:
                    raise StateError("Work transition changed unexpectedly")
            return self._get_work(item.work_kind, item.work_key)
        except sqlite3.Error:
            raise StateError("Unable to complete work") from None

    def _close(self) -> None:
        try:
            if self._db is not None:
                self._db.close()
                self._db = None
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
