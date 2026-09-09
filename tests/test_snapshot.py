import os
from pathlib import Path

import pytest
from ha_syncapp import snapshot as snapshot_module
from ha_syncapp.snapshot import SnapshotError, capture_snapshot, verify_snapshot


def test_nested_tree_is_captured_byte_for_byte_with_deterministic_evidence(tmp_path: Path) -> None:
    source = tmp_path / "ha"
    staging = tmp_path / "staging"
    (source / ".storage").mkdir(parents=True)
    (source / "custom_components/demo").mkdir(parents=True)
    staging.mkdir()
    (source / "configuration.yaml").write_bytes(b"homeassistant:\n  name: Test\n")
    (source / "secrets.yaml").write_bytes(b"token: secret-value\n")
    (source / ".storage/core.entity_registry").write_bytes(b'{"data":{"entities":[]}}')
    binary = b"\x00\xff\x01\x80"
    (source / "custom_components/demo/blob.bin").write_bytes(binary)

    first = capture_snapshot(source, staging)
    second = capture_snapshot(source, staging)

    assert first.snapshot_id == second.snapshot_id
    assert [entry.path for entry in first.files] == sorted(entry.path for entry in first.files)
    assert {entry.path for entry in first.files} == {
        ".storage/core.entity_registry",
        "configuration.yaml",
        "custom_components/demo/blob.bin",
        "secrets.yaml",
    }
    assert (first.tree_path / "custom_components/demo/blob.bin").read_bytes() == binary
    assert (first.tree_path / "secrets.yaml").read_bytes() == b"token: secret-value\n"
    assert first.manifest_path.parent == first.root
    assert first.tree_path.parent == first.root
    assert verify_snapshot(first.root) == first
    assert verify_snapshot(second.root) == second


def test_source_and_staging_must_be_separate_non_overlapping_directories(tmp_path: Path) -> None:
    source = tmp_path / "ha"
    source.mkdir()
    (source / "configuration.yaml").write_text("x: 1\n")

    with pytest.raises(SnapshotError):
        capture_snapshot(source, source)

    inside = source / "staging"
    inside.mkdir()
    with pytest.raises(SnapshotError):
        capture_snapshot(source, inside)

    outside = tmp_path / "outer"
    outside.mkdir()
    nested_source = outside / "ha"
    nested_source.mkdir()
    with pytest.raises(SnapshotError):
        capture_snapshot(nested_source, outside)


def test_symlinks_hardlinks_and_special_files_fail_closed(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()

    symlink_source = tmp_path / "symlink-source"
    symlink_source.mkdir()
    target = tmp_path / "target"
    target.write_text("preserve")
    (symlink_source / "link").symlink_to(target)
    with pytest.raises(SnapshotError):
        capture_snapshot(symlink_source, staging)

    hardlink_source = tmp_path / "hardlink-source"
    hardlink_source.mkdir()
    original = hardlink_source / "one"
    original.write_text("same inode")
    os.link(original, hardlink_source / "two")
    with pytest.raises(SnapshotError):
        capture_snapshot(hardlink_source, staging)

    fifo_source = tmp_path / "fifo-source"
    fifo_source.mkdir()
    os.mkfifo(fifo_source / "pipe")
    with pytest.raises(SnapshotError):
        capture_snapshot(fifo_source, staging)


def test_source_mutation_during_capture_is_rejected_and_incomplete_stage_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "ha"
    staging = tmp_path / "staging"
    source.mkdir()
    staging.mkdir()
    watched = source / "a.yaml"
    watched.write_text("before")
    (source / "b.yaml").write_text("stable")
    original_copy = snapshot_module._copy_regular_file
    changed = False

    def mutate_after_copy(*args: object, **kwargs: object):
        nonlocal changed
        result = original_copy(*args, **kwargs)
        if not changed:
            watched.write_text("after")
            changed = True
        return result

    monkeypatch.setattr(snapshot_module, "_copy_regular_file", mutate_after_copy)
    with pytest.raises(SnapshotError):
        capture_snapshot(source, staging)
    assert list(staging.iterdir()) == []


def test_source_insertion_and_deletion_during_capture_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for action in ("insert", "delete"):
        case = tmp_path / action
        source = case / "ha"
        staging = case / "staging"
        source.mkdir(parents=True)
        staging.mkdir()
        (source / "a.yaml").write_text("a")
        victim = source / "b.yaml"
        victim.write_text("b")
        original_copy = snapshot_module._copy_regular_file
        changed = False

        def change_tree(
            *args: object,
            _action: str = action,
            _source: Path = source,
            _victim: Path = victim,
            _original_copy=original_copy,
            **kwargs: object,
        ):
            nonlocal changed
            result = _original_copy(*args, **kwargs)
            if not changed:
                if _action == "insert":
                    (_source / "new.yaml").write_text("new")
                else:
                    _victim.unlink()
                changed = True
            return result

        monkeypatch.setattr(snapshot_module, "_copy_regular_file", change_tree)
        with pytest.raises(SnapshotError):
            capture_snapshot(source, staging)
        assert list(staging.iterdir()) == []
        monkeypatch.setattr(snapshot_module, "_copy_regular_file", original_copy)


@pytest.mark.parametrize("tamper", ["modify", "insert", "delete", "symlink"])
def test_staged_tree_tampering_is_detected(tmp_path: Path, tamper: str) -> None:
    source = tmp_path / "ha"
    staging = tmp_path / "staging"
    source.mkdir()
    staging.mkdir()
    (source / "configuration.yaml").write_text("original")
    snapshot = capture_snapshot(source, staging)

    target = snapshot.tree_path / "configuration.yaml"
    if tamper == "modify":
        target.write_text("changed")
    elif tamper == "insert":
        (snapshot.tree_path / "extra").write_text("extra")
    elif tamper == "delete":
        target.unlink()
    else:
        target.unlink()
        target.symlink_to(source / "configuration.yaml")

    with pytest.raises(SnapshotError):
        verify_snapshot(snapshot.root)


def test_manifest_tampering_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "ha"
    staging = tmp_path / "staging"
    source.mkdir()
    staging.mkdir()
    (source / "configuration.yaml").write_text("original")
    snapshot = capture_snapshot(source, staging)
    snapshot.manifest_path.write_text('{"version":1,"snapshot_id":"forged","files":[]}')
    with pytest.raises(SnapshotError):
        verify_snapshot(snapshot.root)
