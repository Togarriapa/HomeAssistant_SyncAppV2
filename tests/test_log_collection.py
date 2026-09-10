from datetime import UTC, datetime
from pathlib import Path

import pytest
from ha_syncapp.log_collection import LogCollectionError, collect_and_enqueue_supervisor_logs
from ha_syncapp.state import StateStore
from ha_syncapp.supervisor_logs import SupervisorLogResponse

REFERENCE = datetime(2026, 9, 10, 4, 50, tzinfo=UTC)
TARGET = "Owner/Private-Home"
TOKEN = "supervisor-secret"


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return store


def test_collects_both_sources_then_stages_and_enqueues_exact_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    calls: list[str] = []

    def transport(method, url, headers, timeout, limit):
        del method, headers, timeout, limit
        calls.append(url)
        body = b"core line\n" if "/core/" in url else b"supervisor line\n"
        return SupervisorLogResponse(200, "text/plain", body)

    try:
        result = collect_and_enqueue_supervisor_logs(
            store,
            root,
            TARGET,
            reference_time=REFERENCE,
            token=TOKEN,
            transport=transport,
        )
        claimed = store.claim_work_kind("logs")
    finally:
        store.__exit__(None, None, None)

    assert len(calls) == 2
    assert result.artifact.root == root / result.artifact.artifact_id
    assert result.work.status == "pending"
    assert claimed is not None
    assert claimed.work_key == result.work.work_key
    assert (result.artifact.root / "logs/home-assistant/records.jsonl").is_file()
    assert (result.artifact.root / "logs/supervisor/records.jsonl").is_file()


def test_required_source_failure_creates_no_artifact_and_no_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)

    def transport(method, url, headers, timeout, limit):
        del method, headers, timeout, limit
        if "/core/" in url:
            return SupervisorLogResponse(200, "text/plain", b"core line\n")
        return SupervisorLogResponse(503, "text/plain", b"failed")

    try:
        with pytest.raises(LogCollectionError, match="failed closed"):
            collect_and_enqueue_supervisor_logs(
                store,
                root,
                TARGET,
                reference_time=REFERENCE,
                token=TOKEN,
                transport=transport,
            )
        claimed = store.claim_work_kind("logs")
        children = list(root.iterdir())
    finally:
        store.__exit__(None, None, None)

    assert claimed is None
    assert children == []


def test_repeated_identical_collection_is_idempotently_enqueued(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)

    def transport(method, url, headers, timeout, limit):
        del method, url, headers, timeout, limit
        return SupervisorLogResponse(200, "text/plain", b"same\n")

    try:
        first = collect_and_enqueue_supervisor_logs(
            store,
            root,
            TARGET,
            reference_time=REFERENCE,
            token=TOKEN,
            transport=transport,
        )
        second = collect_and_enqueue_supervisor_logs(
            store,
            root,
            TARGET,
            reference_time=REFERENCE,
            token=TOKEN,
            transport=transport,
        )
    finally:
        store.__exit__(None, None, None)

    assert first.artifact.artifact_id == second.artifact.artifact_id
    assert first.work.work_key == second.work.work_key


def test_collection_error_does_not_expose_supervisor_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)

    def transport(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(TOKEN)

    try:
        with pytest.raises(LogCollectionError) as captured:
            collect_and_enqueue_supervisor_logs(
                store,
                root,
                TARGET,
                reference_time=REFERENCE,
                token=TOKEN,
                transport=transport,
            )
    finally:
        store.__exit__(None, None, None)

    assert TOKEN not in str(captured.value)
    assert TOKEN not in repr(captured.value)
