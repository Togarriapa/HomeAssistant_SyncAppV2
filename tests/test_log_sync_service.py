from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from ha_syncapp import log_sync_service as log_service
from ha_syncapp import log_sync_work
from ha_syncapp.log_collection import (
    LogCollectionResult,
    collect_and_enqueue_supervisor_logs,
)
from ha_syncapp.log_sync import LogSyncError
from ha_syncapp.log_sync_process import LogSyncProcessResult
from ha_syncapp.state import StateStore
from ha_syncapp.supervisor_logs import SupervisorLogResponse

TARGET = "Owner/Home"
GITHUB_TOKEN = "github-secret-sentinel"
CORE_TOKEN = "supervisor-secret-sentinel"


def _opened_store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    return store


def _service(
    tmp_path: Path, store: StateStore, *, interval: float = 60.0
) -> log_service.LogSyncService:
    return log_service.LogSyncService(
        store,
        tmp_path / "log-artifacts",
        tmp_path / "log-snapshots",
        tmp_path / "log-workspaces",
        TARGET,
        GITHUB_TOKEN,
        core_token=CORE_TOKEN,
        interval_seconds=interval,
    )


def test_log_service_arms_first_deadline_without_immediate_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        log_service,
        "collect_and_enqueue_supervisor_logs",
        lambda *args, **kwargs: calls.append("collect"),
    )
    monkeypatch.setattr(
        log_service,
        "run_log_sync_process",
        lambda *args, **kwargs: calls.append("process") or LogSyncProcessResult(None),
    )
    service = _service(tmp_path, store)
    try:
        service.start(100.0)
        assert service.tick(159.999) == log_service.LogSyncTickResult(
            due=False,
            collection=None,
            processed=None,
        )
        assert calls == []
    finally:
        store.__exit__(None, None, None)


def test_log_service_due_tick_collects_then_processes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[tuple[object, ...]] = []
    collection = cast(LogCollectionResult, object())
    processed = LogSyncProcessResult(None)

    def collect(
        state: StateStore,
        artifact_root: Path,
        target: str,
        *,
        reference_time: datetime,
        token: str | None = None,
        transport: object | None = None,
    ) -> LogCollectionResult:
        del transport
        calls.append(("collect", state, artifact_root, target, reference_time, token))
        return collection

    def process(
        state: StateStore,
        artifact_root: Path,
        snapshot_root: Path,
        workspace_root: Path,
        target: str,
        token: str,
    ) -> LogSyncProcessResult:
        calls.append(
            (
                "process",
                state,
                artifact_root,
                snapshot_root,
                workspace_root,
                target,
                token,
            )
        )
        return processed

    monkeypatch.setattr(log_service, "collect_and_enqueue_supervisor_logs", collect)
    monkeypatch.setattr(log_service, "run_log_sync_process", process)
    service = _service(tmp_path, store)
    try:
        service.start(10.0)
        result = service.tick(70.0)
        assert result == log_service.LogSyncTickResult(
            due=True,
            collection=collection,
            processed=processed,
        )
        assert calls[0][:4] == (
            "collect",
            store,
            tmp_path / "log-artifacts",
            TARGET,
        )
        reference_time = calls[0][4]
        assert isinstance(reference_time, datetime)
        assert reference_time.tzinfo is UTC
        assert calls[0][5] == CORE_TOKEN
        assert calls[1] == (
            "process",
            store,
            tmp_path / "log-artifacts",
            tmp_path / "log-snapshots",
            tmp_path / "log-workspaces",
            TARGET,
            GITHUB_TOKEN,
        )
    finally:
        store.__exit__(None, None, None)


def test_log_service_coalesces_missed_intervals_without_catchup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[str] = []
    collection = cast(LogCollectionResult, object())
    monkeypatch.setattr(
        log_service,
        "collect_and_enqueue_supervisor_logs",
        lambda *args, **kwargs: calls.append("collect") or collection,
    )
    monkeypatch.setattr(
        log_service,
        "run_log_sync_process",
        lambda *args, **kwargs: calls.append("process") or LogSyncProcessResult(None),
    )
    service = _service(tmp_path, store, interval=30.0)
    try:
        service.start(0.0)
        assert service.tick(300.0).due is True
        assert service.tick(300.0).due is False
        assert service.tick(329.999).due is False
        assert service.tick(330.0).due is True
        assert calls == ["collect", "process", "collect", "process"]
    finally:
        store.__exit__(None, None, None)


def test_log_service_rejects_invalid_clock_and_lifecycle(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    service = _service(tmp_path, store)
    try:
        with pytest.raises(log_service.LogSyncServiceError, match="not started"):
            service.tick(0.0)
        with pytest.raises(log_service.LogSyncServiceError, match="clock"):
            service.start(float("nan"))
        service.start(10.0)
        with pytest.raises(log_service.LogSyncServiceError, match="already started"):
            service.start(11.0)
        with pytest.raises(log_service.LogSyncServiceError, match="backwards"):
            service.tick(9.999)
        service.stop()
        with pytest.raises(log_service.LogSyncServiceError, match="not started"):
            service.tick(70.0)
        with pytest.raises(log_service.LogSyncServiceError, match="not started"):
            service.stop()
    finally:
        store.__exit__(None, None, None)


def test_log_service_collection_failure_prevents_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    processed = False

    def fail_collection(*args: object, **kwargs: object) -> LogCollectionResult:
        raise log_service.LogCollectionError("sentinel")

    def forbidden_process(*args: object, **kwargs: object) -> LogSyncProcessResult:
        nonlocal processed
        processed = True
        return LogSyncProcessResult(None)

    monkeypatch.setattr(log_service, "collect_and_enqueue_supervisor_logs", fail_collection)
    monkeypatch.setattr(log_service, "run_log_sync_process", forbidden_process)
    service = _service(tmp_path, store)
    try:
        service.start(0.0)
        with pytest.raises(log_service.LogSyncServiceError, match="tick failed closed"):
            service.tick(60.0)
        assert processed is False
    finally:
        store.__exit__(None, None, None)


def test_log_service_wraps_processing_failure_after_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    collection = cast(LogCollectionResult, object())
    monkeypatch.setattr(
        log_service,
        "collect_and_enqueue_supervisor_logs",
        lambda *args, **kwargs: collection,
    )

    def fail_process(*args: object, **kwargs: object) -> LogSyncProcessResult:
        raise log_service.LogSyncProcessError("sentinel")

    monkeypatch.setattr(log_service, "run_log_sync_process", fail_process)
    service = _service(tmp_path, store)
    try:
        service.start(0.0)
        with pytest.raises(log_service.LogSyncServiceError, match="tick failed closed"):
            service.tick(60.0)
    finally:
        store.__exit__(None, None, None)


def test_log_service_transient_publication_failure_hands_off_durable_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    artifact_root = tmp_path / "log-artifacts"
    artifact_root.mkdir(mode=0o700)

    def collect_with_bounded_transport(
        state: StateStore,
        root: Path,
        target: str,
        *,
        reference_time: datetime,
        token: str | None = None,
    ) -> LogCollectionResult:
        def transport(*args: object) -> SupervisorLogResponse:
            return SupervisorLogResponse(200, "text/plain", b"bounded log line\n")

        return collect_and_enqueue_supervisor_logs(
            state,
            root,
            target,
            reference_time=reference_time,
            token=token,
            transport=transport,
        )

    def fail_publication(*args: object, **kwargs: object) -> None:
        raise LogSyncError("transient transport sentinel")

    monkeypatch.setattr(
        log_service,
        "collect_and_enqueue_supervisor_logs",
        collect_with_bounded_transport,
    )
    monkeypatch.setattr(log_sync_work, "synchronize_log_artifact", fail_publication)
    service = _service(tmp_path, store)
    try:
        service.start(0.0)
        result = service.tick(60.0)
    finally:
        store.__exit__(None, None, None)

    assert result.collection is not None
    assert result.processed is not None
    assert result.processed.processed is not None
    assert result.processed.processed.work.status == "retry"
