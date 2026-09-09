from pathlib import Path

import pytest
from ha_syncapp import retrigger_cycle
from ha_syncapp.database_sync_retrigger import DatabaseSyncRetriggerResult
from ha_syncapp.local_sync_retrigger import LocalSyncRetriggerResult
from ha_syncapp.state import StateStore


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _run(store: StateStore, tmp_path: Path, token: str = "token"):
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
        "Owner/Private-Home",
        token,
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

    monkeypatch.setattr(retrigger_cycle, "run_local_sync_retrigger_pass", local)
    monkeypatch.setattr(retrigger_cycle, "run_database_sync_retrigger_pass", database)
    try:
        result = _run(store, tmp_path)
    finally:
        store.__exit__(None, None, None)

    assert calls == ["local_sync", "database"]
    assert result.local_sync.processed is None
    assert result.database_sync.processed is None


def test_cycle_passes_explicit_inputs_to_both_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    captured: dict[str, tuple[object, ...]] = {}

    def local(*args: object, **kwargs: object) -> LocalSyncRetriggerResult:
        captured["local"] = args
        return LocalSyncRetriggerResult(0, None)

    def database(*args: object, **kwargs: object) -> DatabaseSyncRetriggerResult:
        captured["database"] = args
        return DatabaseSyncRetriggerResult(0, None)

    monkeypatch.setattr(retrigger_cycle, "run_local_sync_retrigger_pass", local)
    monkeypatch.setattr(retrigger_cycle, "run_database_sync_retrigger_pass", database)
    try:
        _run(store, tmp_path, token="secret-token")
    finally:
        store.__exit__(None, None, None)

    assert captured["local"][0] is store
    assert captured["database"][0] is store
    assert captured["local"][-2:] == ("Owner/Private-Home", "secret-token")
    assert captured["database"][-2:] == ("Owner/Private-Home", "secret-token")


def test_cycle_does_not_consume_unimplemented_work_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.enqueue_work("candidate", "candidate-a")
    store.enqueue_work("runtime", "runtime-a")
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
    try:
        _run(store, tmp_path)
        claimed = [store.claim_work(), store.claim_work(), store.claim_work()]
    finally:
        store.__exit__(None, None, None)

    assert {item.work_kind for item in claimed if item is not None} == {
        "candidate",
        "runtime",
        "logs",
    }


def test_cycle_stops_before_database_lane_when_local_lane_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    database_called = False

    def local(*args: object, **kwargs: object) -> LocalSyncRetriggerResult:
        from ha_syncapp.local_sync_retrigger import LocalSyncRetriggerError

        raise LocalSyncRetriggerError("nested sensitive detail")

    def database(*args: object, **kwargs: object) -> DatabaseSyncRetriggerResult:
        nonlocal database_called
        database_called = True
        return DatabaseSyncRetriggerResult(0, None)

    monkeypatch.setattr(retrigger_cycle, "run_local_sync_retrigger_pass", local)
    monkeypatch.setattr(retrigger_cycle, "run_database_sync_retrigger_pass", database)
    try:
        with pytest.raises(retrigger_cycle.RetriggerCycleError, match="failed closed") as error:
            _run(store, tmp_path, token="ghp_super_secret_value")
    finally:
        store.__exit__(None, None, None)

    assert not database_called
    assert "nested sensitive detail" not in str(error.value)
    assert "ghp_super_secret_value" not in str(error.value)


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
            "Owner/Private-Home",
            "token",
        )
