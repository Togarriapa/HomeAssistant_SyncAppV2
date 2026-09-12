import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from ha_syncapp.local_change_service import LocalChangeService, LocalChangeServiceError
from ha_syncapp.local_sync_process import LocalSyncProcessResult
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
TOKEN = "token"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "homeassistant"
    source.mkdir()
    (source / "configuration.yaml").write_text("homeassistant:\n")
    return source


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    snapshot = tmp_path / "snapshots"
    workspace = tmp_path / "workspaces"
    snapshot.mkdir()
    workspace.mkdir()
    return snapshot, workspace


def _blocking_consumer(
    captured_notify: list[Callable[[], None]],
) -> Callable[
    [Path, Callable[[], None], threading.Event, Callable[[], None]],
    int,
]:
    def consume(
        source: Path,
        notify: Callable[[], None],
        stop: threading.Event,
        ready: Callable[[], None],
    ) -> int:
        del source
        captured_notify.append(notify)
        ready()
        stop.wait()
        return 0

    return consume


def test_service_drains_event_into_existing_durable_processor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(tmp_path)
    snapshot, workspace = _roots(tmp_path)
    notify: list[Callable[[], None]] = []
    processed: list[tuple[Path, Path, Path, str, str]] = []

    def processor(
        observed_store: StateStore,
        observed_source: Path,
        observed_snapshot: Path,
        observed_workspace: Path,
        target: str,
        token: str,
    ) -> LocalSyncProcessResult:
        assert observed_store is store
        processed.append(
            (
                observed_source,
                observed_snapshot,
                observed_workspace,
                target,
                token,
            )
        )
        return LocalSyncProcessResult(processed=None)

    try:
        service = LocalChangeService(
            store,
            source,
            snapshot,
            workspace,
            TARGET,
            TOKEN,
            quiet_seconds=1.0,
            consumer=_blocking_consumer(notify),
            processor=processor,
        )
        service.start(10.0)

        (source / "automations.yaml").write_text("[]\n")
        notify[0]()

        assert service.tick(10.5) is None
        result = service.tick(11.5)

        assert result == LocalSyncProcessResult(processed=None)
        assert processed == [(source, snapshot, workspace, TARGET, TOKEN)]
        service.stop()
    finally:
        store.__exit__(None, None, None)


def test_service_rechecks_source_after_transport_readiness(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(tmp_path)
    snapshot, workspace = _roots(tmp_path)
    notify: list[Callable[[], None]] = []
    processed = 0

    def consumer(
        observed_source: Path,
        event_notify: Callable[[], None],
        stop: threading.Event,
        ready: Callable[[], None],
    ) -> int:
        del observed_source
        notify.append(event_notify)
        (source / "scripts.yaml").write_text("{}\n")
        ready()
        stop.wait()
        return 0

    def processor(
        observed_store: StateStore,
        observed_source: Path,
        observed_snapshot: Path,
        observed_workspace: Path,
        target: str,
        token: str,
    ) -> LocalSyncProcessResult:
        nonlocal processed
        del (
            observed_store,
            observed_source,
            observed_snapshot,
            observed_workspace,
            target,
            token,
        )
        processed += 1
        return LocalSyncProcessResult(processed=None)

    try:
        service = LocalChangeService(
            store,
            source,
            snapshot,
            workspace,
            TARGET,
            TOKEN,
            quiet_seconds=1.0,
            consumer=consumer,
            processor=processor,
        )
        service.start(20.0)

        assert service.tick(20.999) is None
        assert service.tick(21.0) == LocalSyncProcessResult(processed=None)
        assert processed == 1
        service.stop()
    finally:
        store.__exit__(None, None, None)


def test_change_during_processing_remains_bounded_follow_up_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(tmp_path)
    snapshot, workspace = _roots(tmp_path)
    notify: list[Callable[[], None]] = []
    calls = 0

    def processor(
        observed_store: StateStore,
        observed_source: Path,
        observed_snapshot: Path,
        observed_workspace: Path,
        target: str,
        token: str,
    ) -> LocalSyncProcessResult:
        nonlocal calls
        del (
            observed_store,
            observed_source,
            observed_snapshot,
            observed_workspace,
            target,
            token,
        )
        calls += 1
        if calls == 1:
            (source / "scripts.yaml").write_text("{}\n")
            for _ in range(100):
                notify[0]()
        return LocalSyncProcessResult(processed=None)

    try:
        service = LocalChangeService(
            store,
            source,
            snapshot,
            workspace,
            TARGET,
            TOKEN,
            quiet_seconds=1.0,
            consumer=_blocking_consumer(notify),
            processor=processor,
        )
        service.start(30.0)
        (source / "automations.yaml").write_text("[]\n")
        notify[0]()

        assert service.tick(30.0) is None
        assert service.tick(31.0) == LocalSyncProcessResult(processed=None)
        assert calls == 1

        assert service.tick(31.1) is None
        assert service.tick(32.1) == LocalSyncProcessResult(processed=None)
        assert calls == 2
        service.stop()
    finally:
        store.__exit__(None, None, None)


def test_service_fails_closed_if_event_transport_stops(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = _source(tmp_path)
    snapshot, workspace = _roots(tmp_path)

    def consumer(
        observed_source: Path,
        notify: Callable[[], None],
        stop: threading.Event,
        ready: Callable[[], None],
    ) -> int:
        del observed_source, notify, stop
        ready()
        return 0

    try:
        service = LocalChangeService(
            store,
            source,
            snapshot,
            workspace,
            TARGET,
            TOKEN,
            quiet_seconds=1.0,
            consumer=consumer,
        )
        service.start(40.0)

        with pytest.raises(LocalChangeServiceError, match="stopped unexpectedly"):
            service.tick(40.1)
    finally:
        store.__exit__(None, None, None)
