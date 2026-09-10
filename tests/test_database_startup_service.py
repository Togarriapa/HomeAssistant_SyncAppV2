import json
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp.config import Config
from ha_syncapp.database_startup import DatabaseStartupError, DatabaseStartupResult
from ha_syncapp.github_repo import RepoIdentity
from ha_syncapp.state import StateStore

TARGET = "Owner/Home"
TOKEN = "github-secret-sentinel"


def _opened_store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    return store


def test_database_bootstrap_requires_explicit_recorder_path(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    try:
        assert (
            service._run_startup_database_if_configured(
                store,
                Config(repo_b=TARGET, github_token=TOKEN),
                tmp_path / "data",
                tmp_path / "homeassistant",
            )
            is None
        )
    finally:
        store.__exit__(None, None, None)


def test_database_bootstrap_uses_private_roots_after_canonical_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "homeassistant"
    home.mkdir()
    source = home / "recorder.db"
    source.write_bytes(b"sqlite-placeholder")
    store = _opened_store(tmp_path)
    captured: dict[str, object] = {}

    def bootstrap(
        state: StateStore,
        source_database: Path,
        database_staging_root: Path,
        snapshot_staging_root: Path,
        workspace_root: Path,
        target: str,
        github_token: str,
    ) -> DatabaseStartupResult:
        captured.update(
            state=state,
            source=source_database,
            database_staging=database_staging_root,
            snapshot_staging=snapshot_staging_root,
            workspace=workspace_root,
            target=target,
            token=github_token,
        )
        raise DatabaseStartupError("sentinel-after-capture")

    monkeypatch.setattr(service, "run_startup_database_sync", bootstrap)
    config = Config(repo_b=TARGET, github_token=TOKEN, recorder_database_path=str(source))
    try:
        with pytest.raises(DatabaseStartupError, match="sentinel-after-capture"):
            service._run_startup_database_if_configured(store, config, tmp_path / "data", home)
    finally:
        store.__exit__(None, None, None)

    work = tmp_path / "data" / "syncapp" / "work"
    assert captured == {
        "state": store,
        "source": source,
        "database_staging": work / "database-staging",
        "snapshot_staging": work / "database-snapshots",
        "workspace": work / "database-workspaces",
        "target": TARGET,
        "token": TOKEN,
    }
    for name in ("database-staging", "database-snapshots", "database-workspaces"):
        assert (work / name).is_dir()
        assert (work / name).stat().st_mode & 0o777 == 0o700


def test_database_bootstrap_rejects_parent_symlink_escape(tmp_path: Path) -> None:
    home = tmp_path / "homeassistant"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "recorder.db"
    source.write_bytes(b"sqlite-placeholder")
    (home / "linked").symlink_to(outside, target_is_directory=True)
    store = _opened_store(tmp_path)
    config = Config(
        repo_b=TARGET,
        github_token=TOKEN,
        recorder_database_path=str(home / "linked" / "recorder.db"),
    )
    try:
        with pytest.raises(DatabaseStartupError, match="escapes Home Assistant"):
            service._run_startup_database_if_configured(store, config, tmp_path / "data", home)
    finally:
        store.__exit__(None, None, None)


def test_database_bootstrap_rejects_unavailable_source_without_scheduling(
    tmp_path: Path,
) -> None:
    home = tmp_path / "homeassistant"
    home.mkdir()
    store = _opened_store(tmp_path)
    config = Config(
        repo_b=TARGET,
        github_token=TOKEN,
        recorder_database_path=str(home / "missing.db"),
    )
    try:
        with pytest.raises(DatabaseStartupError, match="failed closed"):
            service._run_startup_database_if_configured(store, config, tmp_path / "data", home)
        assert store.get_work("database", "0" * 64) is None
    finally:
        store.__exit__(None, None, None)


def test_service_orders_database_between_local_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = "/homeassistant/recorder.db"
    (tmp_path / "options.json").write_text(
        json.dumps(
            {
                "repo_b": TARGET,
                "github_token": TOKEN,
                "recorder_database_path": recorder,
            }
        )
    )
    order: list[str] = []
    stop_after_runtime = service.Shutdown()

    def verify(target: str, token: str, *, expected_id: int | None = None) -> RepoIdentity:
        assert target == TARGET
        assert token == TOKEN
        order.append("trust")
        return RepoIdentity(target=target, repository_id=123)

    def local(*args: object, **kwargs: object) -> None:
        order.append("local")

    def database(store: StateStore, config: Config, data_dir: Path) -> None:
        assert store.repository_id(TARGET) == 123
        assert config.recorder_database_path == recorder
        assert data_dir == tmp_path
        order.append("database")

    def runtime(*args: object, **kwargs: object) -> None:
        order.append("runtime")
        stop_after_runtime.requested = True

    monkeypatch.setattr(service, "fetch_and_verify_private_repository", verify)
    monkeypatch.setattr(service, "_run_startup_local_if_configured", local)
    monkeypatch.setattr(service, "_run_startup_database_if_configured", database)
    monkeypatch.setattr(service, "_run_startup_runtime_if_configured", runtime)

    service.run(tmp_path, stop_after_runtime)
    assert order == ["trust", "local", "database", "runtime"]


def test_shutdown_after_database_bootstrap_skips_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "options.json").write_text(
        json.dumps(
            {
                "repo_b": TARGET,
                "github_token": TOKEN,
                "recorder_database_path": "/homeassistant/recorder.db",
            }
        )
    )
    stop_after_database = service.Shutdown()

    monkeypatch.setattr(
        service,
        "fetch_and_verify_private_repository",
        lambda target, token, expected_id=None: RepoIdentity(target=target, repository_id=123),
    )
    monkeypatch.setattr(service, "_run_startup_local_if_configured", lambda *args: None)

    def database(*args: object, **kwargs: object) -> None:
        stop_after_database.requested = True

    def forbidden_runtime(*args: object, **kwargs: object) -> None:
        pytest.fail("shutdown after database bootstrap must skip runtime startup")

    monkeypatch.setattr(service, "_run_startup_database_if_configured", database)
    monkeypatch.setattr(service, "_run_startup_runtime_if_configured", forbidden_runtime)

    service.run(tmp_path, stop_after_database)
