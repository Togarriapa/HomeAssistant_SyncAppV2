from __future__ import annotations

from pathlib import Path

import pytest
from ha_syncapp import retrigger_cycle
from ha_syncapp.state import StateStore


def test_retrigger_cycle_skips_only_database_lane_when_source_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    calls: list[str] = []

    monkeypatch.setattr(
        retrigger_cycle,
        "run_local_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("local") or object(),
    )

    def forbidden_database(*args: object, **kwargs: object) -> object:
        raise AssertionError("database lane must be skipped")

    monkeypatch.setattr(
        retrigger_cycle,
        "run_database_sync_retrigger_pass",
        forbidden_database,
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "run_runtime_sync_retrigger_pass",
        lambda *args, **kwargs: calls.append("runtime") or object(),
    )
    monkeypatch.setattr(
        retrigger_cycle,
        "detect_and_enqueue_trusted_candidate",
        lambda *args, **kwargs: calls.append("candidate") or object(),
    )

    try:
        result = retrigger_cycle.run_retrigger_cycle(
            store,
            tmp_path / "homeassistant",
            tmp_path / "main-snapshots",
            tmp_path / "main-workspaces",
            None,
            tmp_path / "database-staging",
            tmp_path / "database-snapshots",
            tmp_path / "database-workspaces",
            tmp_path / "runtime-staging",
            tmp_path / "runtime-snapshots",
            tmp_path / "runtime-workspaces",
            "Owner/Private-Home",
            "test-token",
        )
    finally:
        store.__exit__(None, None, None)

    assert calls == ["local", "runtime", "candidate"]
    assert result.database_sync.recovered_interrupted == 0
    assert result.database_sync.processed is None
