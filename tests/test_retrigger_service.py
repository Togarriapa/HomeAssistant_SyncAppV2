from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from ha_syncapp import __main__ as app
from ha_syncapp.config import Config
from ha_syncapp.github_repo import RepoIdentity, RepositoryVerificationError
from ha_syncapp.retrigger_ipc import RetriggerRequest
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
GIT_CREDENTIAL = "test-credential"
CORE_CREDENTIAL = "test-core-credential"


def _opened_store(tmp_path: Path) -> tuple[Path, StateStore]:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return data, store


def _request(tmp_path: Path) -> RetriggerRequest:
    home = tmp_path / "homeassistant"
    home.mkdir(exist_ok=True)
    database = home / "home-assistant_v2.db"
    database.touch(exist_ok=True)
    return RetriggerRequest(home.resolve(), database.resolve())


def test_retrigger_request_requires_repo_configuration_before_any_work(tmp_path: Path) -> None:
    data, store = _opened_store(tmp_path)
    request = _request(tmp_path)
    try:
        result = app._handle_retrigger_request(store, Config(), data, request)
    finally:
        store.__exit__(None, None, None)

    assert result == "configuration_invalid"


def test_retrigger_request_requires_existing_trusted_repository_binding(tmp_path: Path) -> None:
    data, store = _opened_store(tmp_path)
    request = _request(tmp_path)
    config = Config(repo_b=TARGET, github_token=GIT_CREDENTIAL)
    try:
        result = app._handle_retrigger_request(store, config, data, request)
    finally:
        store.__exit__(None, None, None)

    assert result == "repo_b_untrusted"


def test_retrigger_reverifies_repo_and_forwards_isolated_paths_and_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, store = _opened_store(tmp_path)
    request = _request(tmp_path)
    store.bind_repository(TARGET, REPOSITORY_ID)
    captured: dict[str, object] = {}

    def verify(target: str, token: str, *, expected_id: int | None = None) -> RepoIdentity:
        captured["verify"] = (target, token, expected_id)
        return RepoIdentity(TARGET, REPOSITORY_ID)

    def cycle(*args: object, **kwargs: object) -> object:
        captured["cycle_args"] = args
        captured["cycle_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(app, "fetch_and_verify_private_repository", verify)
    monkeypatch.setattr(app, "run_retrigger_cycle", cycle)
    monkeypatch.setenv("SUPERVISOR_TOKEN", CORE_CREDENTIAL)
    try:
        result = app._handle_retrigger_request(
            store,
            Config(repo_b=TARGET, github_token=GIT_CREDENTIAL),
            data,
            request,
        )
    finally:
        store.__exit__(None, None, None)

    assert result == "completed"
    assert captured["verify"] == (TARGET, GIT_CREDENTIAL, REPOSITORY_ID)
    args = cast(tuple[object, ...], captured["cycle_args"])
    kwargs = cast(dict[str, object], captured["cycle_kwargs"])
    assert args[0] is store
    assert args[1] == request.home_assistant_root
    assert args[4] == request.recorder_database
    assert args[-2:] == (TARGET, GIT_CREDENTIAL)
    assert kwargs["core_token"] == CORE_CREDENTIAL
    assert kwargs["log_artifact_root"] == data / "syncapp/work/log-artifacts"
    assert kwargs["log_snapshot_root"] == data / "syncapp/work/log-snapshots"
    assert kwargs["log_workspace_root"] == data / "syncapp/work/log-workspaces"

    keyword_work_paths = [
        cast(Path, kwargs["log_artifact_root"]),
        cast(Path, kwargs["log_snapshot_root"]),
        cast(Path, kwargs["log_workspace_root"]),
    ]
    work_paths = [cast(Path, path).resolve(strict=False) for path in args[2:4] + args[5:-2]]
    work_paths += [path.resolve(strict=False) for path in keyword_work_paths]
    protected = (data / "syncapp").resolve()
    home = request.home_assistant_root.resolve()
    assert all(protected in path.parents for path in work_paths)
    assert all(
        home != path and home not in path.parents and path not in home.parents
        for path in work_paths
    )
    assert CORE_CREDENTIAL not in args


def test_repo_reverification_failure_stops_before_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, store = _opened_store(tmp_path)
    request = _request(tmp_path)
    store.bind_repository(TARGET, REPOSITORY_ID)
    called = False

    def verify(*args: object, **kwargs: object) -> RepoIdentity:
        raise RepositoryVerificationError("nested transport detail")

    def cycle(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(app, "fetch_and_verify_private_repository", verify)
    monkeypatch.setattr(app, "run_retrigger_cycle", cycle)
    try:
        result = app._handle_retrigger_request(
            store,
            Config(repo_b=TARGET, github_token=GIT_CREDENTIAL),
            data,
            request,
        )
    finally:
        store.__exit__(None, None, None)

    assert result == "repo_b_untrusted"
    assert called is False


def test_overlapping_protected_and_home_roots_fail_before_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, store = _opened_store(tmp_path)
    store.bind_repository(TARGET, REPOSITORY_ID)
    home = (data / "syncapp").resolve()
    request = RetriggerRequest(home, home / "state.sqlite3")
    called = False

    monkeypatch.setattr(
        app,
        "fetch_and_verify_private_repository",
        lambda *args, **kwargs: RepoIdentity(TARGET, REPOSITORY_ID),
    )

    def cycle(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(app, "run_retrigger_cycle", cycle)
    try:
        result = app._handle_retrigger_request(
            store,
            Config(repo_b=TARGET, github_token=GIT_CREDENTIAL),
            data,
            request,
        )
    finally:
        store.__exit__(None, None, None)

    assert result == "cycle_failed"
    assert called is False


def test_one_shot_client_requires_both_explicit_source_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = tmp_path / "data"
    data.mkdir()

    result = app._run_retrigger_client(data, Path("/homeassistant"), None)

    assert result == 2
    emitted = capsys.readouterr().out
    assert '"event":"retrigger_failed"' in emitted
    assert '"reason":"source_paths_required"' in emitted


def test_one_shot_client_emits_sanitized_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = tmp_path / "data"
    root = data / "syncapp"
    root.mkdir(parents=True)
    root.chmod(0o700)
    captured: dict[str, object] = {}

    def request(socket_path: Path, home: Path, database: Path) -> None:
        captured.update(socket=socket_path, home=home, database=database)

    monkeypatch.setattr(app, "request_retrigger_once", request)
    result = app._run_retrigger_client(
        data,
        Path("/homeassistant"),
        Path("/homeassistant/home-assistant_v2.db"),
    )

    assert result == 0
    assert captured["home"] == Path("/homeassistant")
    assert captured["database"] == Path("/homeassistant/home-assistant_v2.db")
    emitted = capsys.readouterr().out
    assert '"event":"retrigger_completed"' in emitted
    assert "credential" not in emitted.lower()
