from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ha_syncapp.snapshot import SnapshotFile, verify_snapshot


@dataclass(frozen=True, slots=True)
class SnapshotChangeSet:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    modified: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)


def compare_snapshots(before_root: Path, after_root: Path) -> SnapshotChangeSet:
    """Return exact deterministic path changes after re-verifying both snapshots."""
    before = verify_snapshot(before_root)
    after = verify_snapshot(after_root)

    before_files = {entry.path: entry for entry in before.files}
    after_files = {entry.path: entry for entry in after.files}

    before_paths = set(before_files)
    after_paths = set(after_files)
    added = tuple(sorted(after_paths - before_paths))
    removed = tuple(sorted(before_paths - after_paths))
    modified = tuple(
        path
        for path in sorted(before_paths & after_paths)
        if _meaningfully_changed(before_files[path], after_files[path])
    )
    return SnapshotChangeSet(added=added, removed=removed, modified=modified)


def _meaningfully_changed(before: SnapshotFile, after: SnapshotFile) -> bool:
    return (before.size, before.mode, before.sha256) != (after.size, after.mode, after.sha256)
