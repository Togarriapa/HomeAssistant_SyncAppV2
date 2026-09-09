# Consistent Recorder database snapshots

The initial V2 README requires the dedicated Repo B `database` history to contain
safe database snapshots rather than blind copies of files that may be changing
under Home Assistant Recorder.

`capture_sqlite_snapshot()` is the consistency primitive for SQLite Recorder
databases. It opens the source read-only, holds source inode identity evidence,
and uses SQLite's online backup API so committed state represented through an
active WAL is copied into one consistent destination database. The destination
is created only beneath an explicit isolated staging root.

Before success, V2 revalidates the source identity, closes the SQLite backup,
flushes the staged database, runs SQLite `PRAGMA quick_check`, and computes a
stable SHA-256 digest and byte size. Symlinks, hard links, non-regular files,
source identity changes, unsafe staging roots, source-tree overlap, invalid
SQLite content, and integrity failures all fail closed. Incomplete staging is
removed on failure.

This primitive does not publish the `database` branch, choose backup frequency,
prune retention, restore Recorder data, or manipulate Home Assistant. Those are
separate increments. The caller is also responsible for selecting the actual
Recorder database path; custom Recorder locations must be discovered or
configured explicitly rather than guessed.
