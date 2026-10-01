from pathlib import Path

import pytest
from ha_syncapp import local_sync, main_routing
from ha_syncapp import snapshot as snapshot_module


@pytest.mark.parametrize(
    "path",
    [
        "home-assistant_v2.db",
        "home-assistant_v2.db-wal",
        "home-assistant_v2.db-shm",
        "home-assistant_v2.db-journal",
        "home-assistant.log",
        "home-assistant.log.1",
        "home-assistant.log.fault",
    ],
)
def test_standard_dedicated_artifacts_are_not_routed_to_main(path: str) -> None:
    assert main_routing.include_in_main(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "configuration.yaml",
        "secrets.yaml",
        ".storage/core.entity_registry",
        "custom_components/demo/data.db",
        "packages/notes.log",
        "nested/home-assistant_v2.db",
        "nested/home-assistant.log",
    ],
)
def test_unrelated_configuration_paths_remain_in_main(path: str) -> None:
    assert main_routing.include_in_main(path) is True


def test_configured_recorder_family_is_not_routed_to_main(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    recorder = source / "storage" / "recorder.db"

    include = main_routing.build_main_path_router(source, recorder)

    assert include("storage/recorder.db") is False
    assert include("storage/recorder.db-wal") is False
    assert include("storage/recorder.db-shm") is False
    assert include("storage/recorder.db-journal") is False
    assert include("custom_components/demo/data.db") is True
    assert include("storage/recorder.db.notes") is True


@pytest.mark.parametrize(
    "recorder",
    [
        Path("relative/recorder.db"),
        Path("/homeassistant"),
        Path("/outside/recorder.db"),
        Path("/homeassistant/../outside/recorder.db"),
    ],
)
def test_configured_recorder_route_rejects_invalid_path(recorder: Path) -> None:
    with pytest.raises(ValueError, match="configured Recorder path is invalid"):
        main_routing.build_main_path_router(Path("/homeassistant"), recorder)


def test_main_snapshot_preserves_config_and_omits_dedicated_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    staging = tmp_path / "snapshots"
    (source / ".storage").mkdir(parents=True)
    (source / "custom_components/demo").mkdir(parents=True)
    staging.mkdir()
    (source / "configuration.yaml").write_bytes(b"homeassistant:\n")
    (source / "secrets.yaml").write_bytes(b"token: private\n")
    (source / ".storage/core.entity_registry").write_bytes(b"{}")
    (source / "custom_components/demo/data.db").write_bytes(b"config-db")
    (source / "home-assistant_v2.db").write_bytes(b"recorder")
    (source / "home-assistant_v2.db-wal").write_bytes(b"wal")
    (source / "home-assistant_v2.db-shm").write_bytes(b"shm")
    (source / "home-assistant.log").write_bytes(b"log")
    (source / "home-assistant.log.1").write_bytes(b"old-log")

    captured = local_sync.capture_snapshot(source, staging)
    paths = {entry.path for entry in captured.files}

    assert paths == {
        ".storage/core.entity_registry",
        "configuration.yaml",
        "custom_components/demo/data.db",
        "secrets.yaml",
    }
    assert (captured.tree_path / "secrets.yaml").read_bytes() == b"token: private\n"
    assert (captured.tree_path / "custom_components/demo/data.db").read_bytes() == b"config-db"


def test_main_snapshot_omits_configured_recorder_family(tmp_path: Path) -> None:
    source = tmp_path / "homeassistant"
    staging = tmp_path / "snapshots"
    recorder = source / "storage" / "recorder.db"
    recorder.parent.mkdir(parents=True)
    staging.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    recorder.write_bytes(b"recorder")
    recorder.with_name("recorder.db-wal").write_bytes(b"wal")
    (source / "custom_components").mkdir()
    (source / "custom_components/data.db").write_bytes(b"configuration-data")

    captured = local_sync.capture_snapshot(
        source,
        staging,
        recorder_database=recorder,
    )

    assert tuple(entry.path for entry in captured.files) == (
        "configuration.yaml",
        "custom_components/data.db",
    )


def test_excluded_database_churn_does_not_destabilize_main_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "homeassistant"
    staging = tmp_path / "snapshots"
    source.mkdir()
    staging.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    database = source / "home-assistant_v2.db-wal"
    database.write_bytes(b"before")
    original_copy = snapshot_module._copy_regular_file
    changed = False

    def mutate_excluded_after_copy(*args: object, **kwargs: object) -> str:
        nonlocal changed
        digest = original_copy(*args, **kwargs)
        if not changed:
            database.write_bytes(b"after")
            changed = True
        return digest

    monkeypatch.setattr(snapshot_module, "_copy_regular_file", mutate_excluded_after_copy)

    captured = local_sync.capture_snapshot(source, staging)

    assert [entry.path for entry in captured.files] == ["configuration.yaml"]
    assert database.read_bytes() == b"after"


def test_included_configuration_churn_still_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "homeassistant"
    staging = tmp_path / "snapshots"
    source.mkdir()
    staging.mkdir()
    configuration = source / "configuration.yaml"
    configuration.write_text("before")
    (source / "home-assistant_v2.db").write_bytes(b"ignored")
    original_copy = snapshot_module._copy_regular_file
    changed = False

    def mutate_included_after_copy(*args: object, **kwargs: object) -> str:
        nonlocal changed
        digest = original_copy(*args, **kwargs)
        if not changed:
            configuration.write_text("after")
            changed = True
        return digest

    monkeypatch.setattr(snapshot_module, "_copy_regular_file", mutate_included_after_copy)

    with pytest.raises(snapshot_module.SnapshotError, match="source tree changed"):
        local_sync.capture_snapshot(source, staging)

    assert list(staging.iterdir()) == []


def test_selector_failures_are_sanitized_and_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    staging = tmp_path / "staging"
    source.mkdir()
    staging.mkdir()
    (source / "configuration.yaml").write_text("x: 1\n")

    def fail(path: str) -> bool:
        raise RuntimeError(f"unexpected selector failure for {path}")

    with pytest.raises(snapshot_module.SnapshotError, match="path selection failed") as error:
        snapshot_module.capture_snapshot(source, staging, include_path=fail)

    assert "unexpected selector" not in str(error.value)
    assert list(staging.iterdir()) == []
