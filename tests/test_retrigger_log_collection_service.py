from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from ha_syncapp import __main__ as app
from ha_syncapp.config import Config
from ha_syncapp.github_repo import RepoIdentity
from ha_syncapp.retrigger_cycle import RetriggerCycleError
from ha_syncapp.retrigger_ipc import RetriggerRequest
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123


def _opened_store(tmp_path: Path) -> tuple[Path, StateStore]:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return data, store


def _request(tmp_path: Path) -> RetriggerRequest:
    home = tmp_path / "homeassistant"
    home.mkdir(exist_ok=True)
    database = home / "home-assistant_v2.db"
    database.touch(exist_ok=True)
    return RetriggerRequest(home.resolve(), database.resolve())


def _stub_repo_and_cycle(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(
        app,
        "fetch_and_verify_private_repository",
        lambda *args, **kwargs: RepoIdentity(TARGET, REPOSITORY_ID),
    )
    monkeypatch.setattr(
        app,
        "run_retrigger_cycle",
        lambda *args, **kwargs: calls.append("cycle") or object(),
    )


def test_service_creates_private_log_artifact_root_before_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, store = _opened_store(tmp_path)
    request = _request(tmp_path)
    calls: list[str] = []
    _stub_repo_and_cycle(monkeypatch, calls)
    try:
        result = app._handle_retrigger_request(
            store,
            Config(repo_b=TARGET, github_token="github-token"),
            data,
            request,
        )
    finally:
        store.__exit__(None, None, None)

    artifact_root = data / "syncapp/work/log-artifacts"
    assert result == "completed"
    assert calls == ["cycle"]
    assert artifact_root.is_dir()
    info = artifact_root.stat()
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert info.st_uid == os.geteuid()
    assert stat.S_IMODE(artifact_root.parent.stat().st_mode) == 0o700


def test_service_reuses_existing_private_log_artifact_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, store = _opened_store(tmp_path)
    request = _request(tmp_path)
    artifact_root = data / "syncapp/work/log-artifacts"
    artifact_root.mkdir(parents=True, mode=0o700)
    artifact_root.chmod(0o700)
    original_inode = artifact_root.stat().st_ino
    calls: list[str] = []
    _stub_repo_and_cycle(monkeypatch, calls)
    try:
        result = app._handle_retrigger_request(
            store,
            Config(repo_b=TARGET, github_token="github-token"),
            data,
            request,
        )
    finally:
        store.__exit__(None, None, None)

    assert result == "completed"
    assert calls == ["cycle"]
    assert artifact_root.stat().st_ino == original_inode
    assert stat.S_IMODE(artifact_root.stat().st_mode) == 0o700


def test_service_rejects_symlinked_log_artifact_root_before_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, store = _opened_store(tmp_path)
    request = _request(tmp_path)
    work = data / "syncapp/work"
    work.mkdir(mode=0o700)
    external = tmp_path / "external"
    external.mkdir()
    (work / "log-artifacts").symlink_to(external, target_is_directory=True)
    calls: list[str] = []
    _stub_repo_and_cycle(monkeypatch, calls)
    try:
        result = app._handle_retrigger_request(
            store,
            Config(repo_b=TARGET, github_token="github-token"),
            data,
            request,
        )
    finally:
        store.__exit__(None, None, None)

    assert result == "cycle_failed"
    assert calls == []


def test_private_work_directory_rejects_foreign_owner_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = tmp_path / "syncapp"
    protected.mkdir(mode=0o700)
    directory = protected / "work"
    real_fstat = app.os.fstat

    class ForeignDirectory:
        st_mode = stat.S_IFDIR | 0o700
        st_uid = os.geteuid() + 1

    def fstat(descriptor: int) -> os.stat_result | ForeignDirectory:
        del descriptor
        return ForeignDirectory()

    monkeypatch.setattr(app.os, "fstat", fstat)
    try:
        with pytest.raises(RetriggerCycleError, match="work directory is unsafe"):
            app._ensure_private_work_directory(protected, directory)
    finally:
        monkeypatch.setattr(app.os, "fstat", real_fstat)


def test_private_work_directory_rejects_path_outside_protected_storage(tmp_path: Path) -> None:
    protected = tmp_path / "syncapp"
    protected.mkdir(mode=0o700)

    with pytest.raises(RetriggerCycleError, match="escapes protected storage"):
        app._ensure_private_work_directory(protected, tmp_path / "outside")
