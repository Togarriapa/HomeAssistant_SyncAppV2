"""Exclusive, versioned lifecycle state in app-owned storage.

The process lock is held until the SQLite connection closes. Its file must never
be deleted to recover a lock: the kernel releases flock when the process exits.
"""

import fcntl
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import UUID, uuid4

SCHEMA_VERSION = 1


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


class StateStore:
    """One service instance owns one store for its entire lifetime."""

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

    def _open_database(self) -> None:
        path = self._root / "state.sqlite3"
        # Check sidecars before SQLite can follow them during crash recovery.
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
        if version != SCHEMA_VERSION and not (created and version == 0):
            raise StateError("Unsupported state schema")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise StateError("State integrity check failed")
        db.execute("PRAGMA synchronous = FULL")
        if created:
            with db:
                # Explicit BEGIN keeps DDL, seed and schema version atomic.
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE installation ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "installation_id TEXT NOT NULL, "
                    "boot_count INTEGER NOT NULL CHECK (boot_count >= 0), "
                    "active_run_id TEXT, last_started_at TEXT, last_stopped_at TEXT)"
                )
                db.execute(
                    "INSERT INTO installation VALUES (1, ?, 0, NULL, NULL, NULL)", (str(uuid4()),)
                )
                db.execute("PRAGMA user_version = 1")
            # Persist the new file's directory entry as well as its contents.
            root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
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

    def _close(self) -> None:
        try:
            if self._db is not None:
                self._db.close()
                self._db = None
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
