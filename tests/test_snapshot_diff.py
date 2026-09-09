import os
from pathlib import Path

import pytest
from ha_syncapp.snapshot import SnapshotError, capture_snapshot
from ha_syncapp.snapshot_diff import SnapshotChangeSet, compare_snapshots


def _capture(source: Path, staging: Path):
    source.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    return capture_snapshot(source, staging)


def test_identical_snapshots_produce_explicit_empty_change_set(tmp_path: Path) -> None:
    source = tmp_path / "ha"
    staging = tmp_path / "staging"
    source.mkdir()
    staging.mkdir()
    (source / "configuration.yaml").write_bytes(b"homeassistant:\n")

    before = capture_snapshot(source, staging)
    after = capture_snapshot(source, staging)

    assert compare_snapshots(before.root, after.root) == SnapshotChangeSet((), (), ())
    assert compare_snapshots(before.root, after.root).is_empty


def test_added_removed_and_modified_paths_are_reported_deterministically(tmp_path: Path) -> None:
    source = tmp_path / "ha"
    staging = tmp_path / "staging"
    source.mkdir()
    staging.mkdir()
    (source / "z-removed.yaml").write_text("old")
    (source / "m-modified.yaml").write_text("before")
    (source / "unchanged.yaml").write_text("same")
    before = capture_snapshot(source, staging)

    (source / "z-removed.yaml").unlink()
    (source / "m-modified.yaml").write_text("after")
    (source / "a-added.yaml").write_text("new")
    after = capture_snapshot(source, staging)

    changes = compare_snapshots(before.root, after.root)

    assert changes.added == ("a-added.yaml",)
    assert changes.removed == ("z-removed.yaml",)
    assert changes.modified == ("m-modified.yaml",)
    assert not changes.is_empty


def test_mode_only_change_is_reported_as_modified(tmp_path: Path) -> None:
    source = tmp_path / "ha"
    staging = tmp_path / "staging"
    source.mkdir()
    staging.mkdir()
    path = source / "script.sh"
    path.write_bytes(b"echo ok\n")
    os.chmod(path, 0o600)
    before = capture_snapshot(source, staging)

    os.chmod(path, 0o700)
    after = capture_snapshot(source, staging)

    assert compare_snapshots(before.root, after.root).modified == ("script.sh",)


def test_comparison_reverifies_both_inputs_and_fails_closed_on_tamper(tmp_path: Path) -> None:
    left_source = tmp_path / "left-source"
    left_staging = tmp_path / "left-staging"
    right_source = tmp_path / "right-source"
    right_staging = tmp_path / "right-staging"
    (left_source / "nested").mkdir(parents=True)
    (right_source / "nested").mkdir(parents=True)
    left_staging.mkdir()
    right_staging.mkdir()
    (left_source / "nested/value").write_text("same")
    (right_source / "nested/value").write_text("same")
    left = capture_snapshot(left_source, left_staging)
    right = capture_snapshot(right_source, right_staging)

    (left.tree_path / "nested/value").write_text("tampered")
    with pytest.raises(SnapshotError):
        compare_snapshots(left.root, right.root)

    (left.tree_path / "nested/value").write_text("same")
    (right.tree_path / "nested/value").write_text("tampered")
    with pytest.raises(SnapshotError):
        compare_snapshots(left.root, right.root)
