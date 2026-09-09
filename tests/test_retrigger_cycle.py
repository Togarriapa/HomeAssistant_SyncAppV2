from pathlib import Path

import pytest
from ha_syncapp import retrigger_cycle
from ha_syncapp.database_sync_retrigger import DatabaseSyncRetriggerResult
from ha_syncapp.local_sync_retrigger import LocalSyncRetriggerResult
from ha_syncapp.runtime_sync_retrigger import RuntimeSyncRetriggerResult
from ha_syncapp.state import StateStore


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _run(
    store: StateStore,
    tmp_path: Path,
    github_token: str = "github-token",
    core_token: str = "core-token",
):
    home = tmp_path / "homeassistant"
    home.mkdir(exist_ok=True)
    database = home / "home-assistant_v2.db"
    database.touch(exist_ok=True)
    return retrigger_cycle.run_retrigger_cycle(
        store,
        home,
        tmp_path / "snapshots",
        tmp_path / "local-workspaces",
        database,
        tmp_path / "database-staging",
        tmp_path / "database-snapshots",
        tmp_path / "database-workspaces",
        tmp_path / "runtime-staging",
        tmp_path / "runtime-snapshots",
        tmp_path / "runtime-workspaces",
        "Owner/Private-Home",
        github_token,
        core_token=core_token,
    )


def test_cycle_runs_supported_lanes_in_deterministic_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    calls: list[str] = []

    def local(*args: object, **kwargs: object) -> LocalSyncRetriggerResult:
        calls.append("local_sync")
        return LocalSyncRetriggerResult(0, None)

    def database(*args: object, **kwargs: object) -> DatabaseSyncRetriggerResult:
        calls.append("database")
        return DatabaseSyncRetriggerResult(0, None)

    def runtime(*args: object, **kwargs: object) -> RuntimeSyncRetriggerResult:
        calls.append("runtime")
        return RuntimeSyncRetriggerResult(0, None)

    monkeypatch.setattr(retrigger_cycle, "run_local_sync_retrigger_pass", local)
    monkeypatch.setattr(retrigger_cycle, "run_database_sync_retrigger_pass", database)
    monkeypatch.setattr(retrigger_cycle, "run_runtime_sync_retrigger_pass", runtime)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert calls == ["local_sync", "database", "runtime"]
    assert result.local_sync.processed is None
    assert result.database_sync.processed is None
    assert result.runtime_sync.processed is None


def test_cycle_passes_explicit_inputs_and_separates_core_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    captured: dict[str, tuple[tuple[object, ...], dict[str, object]]] = {}

    def local(*args: object, **kwargs: object) -> LocalSyncRetriggerResult:
        captured["local"] = (args, kwargs)
        return LocalSyncRetriggerResult(0, None)

    def database(*args: object, **kwargs: object) -> DatabaseSyncRetriggerResult:
        captured["database"] = (args, kwargs)
        return DatabaseSyncRetriggerResult(0, None)

    def runtime(*args: object, **kwargs: object) -> RuntimeSyncRetriggerResult:
        captured["runtime"] = (args, kwargs)
        return RuntimeSyncRetriggerResult(0, None)

    monkeypatch.setattr(retrigger_cycle, "run_local_sync_retrigger_pass", local)
    monkeypatch.setattr(retrigger_cycle, "run_database_sync_retrigger_pass", database)
    monkeypatch.setattr(retrigger_cycle, "run_runtime_sync_retrigger_pass", runtime)
    try:
        _run(
            store,
            tmp_path,
            github_token="github-secret",
            core_token="core-secret",
        )
    finally:
        store.__exit__(None, None, None)

    local_args, local_kwargs = captured["local"]
    database_args, database_kwargs = captured["database"]
    runtime_args, runtime_kwargs = captured["runtime"]
    assert local_args[0] is store
    assert database_args[0] is store
    assert runtime_args[0] is store
    assert local_args[-2:] == ("Owner/Private-Home", "github-secret")
    assert database_args[-2:] == ("Owner/Private-Home", "github-secret")
    assert runtime_args[-2:] == ("Owner/Private-Home", "github-secret")
    assert local_kwargs == {}
    assert database_kwargs == {}
    assert runtime_kwargs == {"core_token": "core-secret"}
    assert "core-secret" not in local_args
    assert "core-secret" not in database_args


def test_cycle_does_not_consume_still_unimplemented_work_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.enqueue_work("candidate", "candidate-a")
    store.enqueue_work("logs", "logs-a")

    monkeypatch.setattr(
        retrigger_cycle,
        "run_local_sync_retrigger_pass",
        lambda *args, **kwargs: LocalSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_database_sync_retrigger_pass",
        lambda *args, **kwargs: DatabaseSyncRetriggerResult(0, None),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_runtime_sync_retrigger_pass",
        lambda *args, **kwargs: RuntimeSyncRetriggerResult(0, None),
    )
    try:
        _run(store, tmp_path)
        claimed = [store.claim_work(), store.claim_work()]
    finally:
        store.__exit__(None, None, None)

    assert {item.work_kind for item in claimed if item is not None} == {
        "candidate",
        "logs",
    }


def test_cycle_stops_before_later_lanes_when_local_lane_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    later_calls: list[str] = []

    def local(*args: object, **kwargs: object) -> LocalSyncRetriggerResult:
        from ha_syncapp.local_sync_retrigger import LocalSyncRetriggerError

        raise LocalSyncRetriggerError("nested sensitive detail")

    def database(*args: object, **kwargs: object) -> DatabaseSyncRetriggerResult:
        later_calls.append("database")
        return DatabaseSyncRetriggerResult(0, None)

    def runtime(*args: object, **kwargs: object) -> RuntimeSyncRetriggerResult:
        later_calls.append("runtime")
        return RuntimeSyncRetriggerResult(0, None)

    monkeypatch.setattr(retrigger_cycle, "run_local_sync_retrigger_pass", local)
    monkeypatch.setattr(retrigger_cycle, "run_database_sync_retrigger_pass", database)
    monkeypatch.setattr(retrigger_cycle, "run_runtime_sync_retrigger_pass", runtime)
    try:
        with pytest.raises(retrigger_cycle.RetriggerCycleError, match="failed closed") as error:
            _run(store, tmp_path, github_token="ghp_super_secret_value")
    finally:
        store.__exit__(None, None, None)

    assert later_calls == []
    assert "nested sensitive detail" not in str(error.value)
    assert "ghp_super_secret_value" not in str(error.value)


def test_cycle_stops_before_runtime_when_database_lane_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    runtime_called = False

    def local(*args: object, **kwargs: object) -> LocalSyncRetriggerResult:
        return LocalSyncRetriggerResult(0, None)

    def database(*args: object, **kwargs: object) -> DatabaseSyncRetriggerResult:
        from ha_syncapp.database_sync_retrigger import DatabaseSyncRetriggerError

        raise DatabaseSyncRetriggerError("database sensitive detail")

    def runtime(*args: object, **kwargs: object) -> RuntimeSyncRetriggerResult:
        nonlocal runtime_called
        runtime_called = True
        return RuntimeSyncRetriggerResult(0, None)

    monkeypatch.setattr(retrigger_cycle, "run_local_sync_retrigger_pass", local)
    monkeypatch.setattr(retrigger_cycle, "run_database_sync_retrigger_pass", database)
    monkeypatch.setattr(retrigger_cycle, "run_runtime_sync_retrigger_pass", runtime)
    try:
        with pytest.raises(retrigger_cycle.RetriggerCycleError, match="failed closed") as error:
            _run(store, tmp_path, core_token="supervisor-secret")
    finally:
        store.__exit__(None, None, None)

    assert runtime_called is False
    assert "database sensitive detail" not in str(error.value)
    assert "supervisor-secret" not in str(error.value)


def test_cycle_fails_closed_for_invalid_state_store(tmp_path: Path) -> None:
    with pytest.raises(retrigger_cycle.RetriggerCycleError, match="state store is invalid"):
        retrigger_cycle.run_retrigger_cycle(
            object(),  # type: ignore[arg-type]
            tmp_path / "homeassistant",
            tmp_path / "snapshots",
            tmp_path / "local-workspaces",
            tmp_path / "homeassistant" / "home-assistant_v2.db",
            tmp_path / "database-staging",
            tmp_path / "database-snapshots",
            tmp_path / "database-workspaces",
            tmp_path / "runtime-staging",
            tmp_path / "runtime-snapshots",
            tmp_path / "runtime-workspaces",
            "Owner/Private-Home",
            "github-token",
            core_token="core-token",
        )
