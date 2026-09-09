import os
from pathlib import Path

import pytest
from ha_syncapp import snapshot
from ha_syncapp.snapshot import SnapshotError, capture_snapshot, verify_snapshot


def _write(root: Path, relative: str, data: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_snapshot_preserves_complete_tree_and_routes_only_explicit_logs(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    files = {
        "configuration.yaml": b"homeassistant:\r\n  name: Test\r\n",
        "automations.yaml": b"[]\n",
        ".storage/core.entity_registry": b'{"data":{"entities":[]}}\n',
        ".storage/auth": b'{"credentials":"synthetic"}\n',
        "secrets.yaml": b"api_key: synthetic-secret\n",
        "ssl/fullchain.pem": b"-----BEGIN CERTIFICATE-----\nsynthetic\n",
        "ssl/privkey.pem": b"-----BEGIN PRIVATE KEY-----\nsynthetic\n",
        "home-assistant_v2.db": b"SQLite format 3\x00\x01\x02",
        "home-assistant_v2.db-wal": b"\x00\xffWAL\x10",
        ".cache/generated.bin": bytes(range(32)),
        "custom_components/demo/blob.bin": b"\x00\x01\xfe\xff",
        "notes.log": b"not an operational log unless explicitly routed\n",
        "home-assistant.log": b"runtime log\n",
    }
    for relative, data in files.items():
        _write(source, relative, data)

    stage = tmp_path / "stage"
    manifest = capture_snapshot(source, stage, log_paths={"home-assistant.log"})

    assert {entry.path for entry in manifest.entries} == set(files)
    for relative, data in files.items():
        route = "logs" if relative == "home-assistant.log" else "main"
        assert (stage / route / relative).read_bytes() == data

    assert not (stage / "main/home-assistant.log").exists()
    assert not (stage / "logs/notes.log").exists()
    assert len(manifest.snapshot_id) == 64
    assert manifest.total_bytes == sum(len(value) for value in files.values())
    verify_snapshot(stage, manifest)


def test_snapshot_is_deterministic_for_identical_content(tmp_path: Path) -> None:
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    for root in (first_source, second_source):
        _write(root, ".storage/core.device_registry", b'{"devices":[]}\n')
        _write(root, "configuration.yaml", b"default_config:\n")

    first = capture_snapshot(first_source, tmp_path / "stage-one")
    second = capture_snapshot(second_source, tmp_path / "stage-two")

    assert first.snapshot_id == second.snapshot_id
    assert first.entries == second.entries


def test_snapshot_rejects_symlinked_content(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("do not follow", encoding="utf-8")
    (source / "linked-secret").symlink_to(outside)

    with pytest.raises(SnapshotError, match="symlink"):
        capture_snapshot(source, tmp_path / "stage")

    assert not (tmp_path / "stage").exists()


def test_snapshot_rejects_hardlinked_content(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    original = source / "configuration.yaml"
    original.write_text("default_config:\n", encoding="utf-8")
    os.link(original, source / "alias.yaml")

    with pytest.raises(SnapshotError, match="hardlink"):
        capture_snapshot(source, tmp_path / "stage")


def test_snapshot_requires_new_staging_root_outside_source(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    _write(source, "configuration.yaml", b"default_config:\n")

    existing = tmp_path / "existing-stage"
    existing.mkdir()
    with pytest.raises(SnapshotError, match="must not already exist"):
        capture_snapshot(source, existing)

    with pytest.raises(SnapshotError, match="outside"):
        capture_snapshot(source, source / ".syncapp-stage")


def test_log_paths_must_be_safe_relative_paths(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()

    with pytest.raises(SnapshotError, match="log path"):
        capture_snapshot(source, tmp_path / "stage", log_paths={"../outside.log"})


def test_snapshot_fails_if_another_file_changes_during_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    _write(source, "a.yaml", b"a: 1\n")
    _write(source, "z.yaml", b"z: 1\n")

    original_copy = snapshot._copy_regular_file
    mutated = False

    def copy_then_mutate(*args: object, **kwargs: object) -> tuple[str, int]:
        nonlocal mutated
        result = original_copy(*args, **kwargs)
        if not mutated:
            mutated = True
            (source / "z.yaml").write_bytes(b"z: 222222\n")
        return result

    monkeypatch.setattr(snapshot, "_copy_regular_file", copy_then_mutate)

    with pytest.raises(SnapshotError, match=r"source (?:tree|file|root) changed"):
        capture_snapshot(source, tmp_path / "stage")

    assert not (tmp_path / "stage").exists()


def test_snapshot_fails_if_a_file_is_added_during_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    _write(source, "a.yaml", b"a: 1\n")

    original_copy = snapshot._copy_regular_file
    added = False

    def copy_then_add(*args: object, **kwargs: object) -> tuple[str, int]:
        nonlocal added
        result = original_copy(*args, **kwargs)
        if not added:
            added = True
            _write(source, ".storage/new-state", b"new\n")
        return result

    monkeypatch.setattr(snapshot, "_copy_regular_file", copy_then_add)

    with pytest.raises(SnapshotError, match=r"source (?:tree|file|root) changed"):
        capture_snapshot(source, tmp_path / "stage")

    assert not (tmp_path / "stage").exists()


def test_verify_snapshot_rejects_staged_byte_tampering(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    _write(source, "secrets.yaml", b"token: synthetic\n")
    stage = tmp_path / "stage"
    manifest = capture_snapshot(source, stage)

    (stage / "main/secrets.yaml").write_bytes(b"token: changed\n")

    with pytest.raises(SnapshotError, match="staged snapshot integrity"):
        verify_snapshot(stage, manifest)


def test_verify_snapshot_rejects_added_or_removed_paths(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    _write(source, "configuration.yaml", b"default_config:\n")
    stage = tmp_path / "stage"
    manifest = capture_snapshot(source, stage)

    _write(stage / "main", ".storage/injected", b"unexpected\n")
    with pytest.raises(SnapshotError, match="staged snapshot integrity"):
        verify_snapshot(stage, manifest)

    (stage / "main/.storage/injected").unlink()
    (stage / "main/configuration.yaml").unlink()
    with pytest.raises(SnapshotError, match="staged snapshot integrity"):
        verify_snapshot(stage, manifest)


def test_verify_snapshot_rejects_route_tampering(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    _write(source, "home-assistant.log", b"log\n")
    stage = tmp_path / "stage"
    manifest = capture_snapshot(source, stage, log_paths={"home-assistant.log"})

    (stage / "main").mkdir()
    (stage / "logs/home-assistant.log").replace(stage / "main/home-assistant.log")

    with pytest.raises(SnapshotError, match="staged snapshot integrity"):
        verify_snapshot(stage, manifest)
